"""
期权天眼 — VRP 反转信号模块 (W1 / Wasserstein)

设计原则（与 SABR 天眼整合）:
- 纯消费者：只读 web_state["latest_sabr_params"] 与 web_state["latest_slices"]，
  不调用任何 DeribitWS 网络方法（不 connect / 不 subscribe / 不发请求），
  因此零新增 WebSocket、零新增订阅，不会触发限流 (429)。
- 复用 sabr.calibrator.sabr_iv 在平滑 SABR surface 上做 Breeden-Litzenberger
  二阶导提取 RND（风险中性分布），绝不对原始 IV 数值微分。
- W1 用一维 Wasserstein 闭式 = 两 CDF 的 L1 距离 ∫|F_P − F_Q| dx。
- 多 tenor（7D 早哨 + 30D 主哨）+ 多 lag（1/3/7 天），jump/trend 分类。

M0 脚手架：模块 + 算法 + 历史快照落盘；实盘交易与 OOS 阈值在 M2/M4 完成。
"""
import json
import time
import logging
import numpy as np
from scipy.stats import norm

from sabr.calibrator import sabr_iv

logger = logging.getLogger(__name__)

# numpy 2.x 移除了 np.trapz，统一用 np.trapezoid（旧版回退）
try:
    from numpy import trapezoid as _trapz
except ImportError:  # numpy < 2.0
    from numpy import trapz as _trapz

# RND 提取网格（固定，保证跨快照同一支撑，W1 才可比）
X_BOUND = 0.9      # log-moneyness 半宽（±0.9 ≈ 价格 ±146% @ forward）
N_GRID = 301


def _bs_call(F, K, T, sigma):
    """无贴现 BS call 价格（RND 形状无需贴现，常数因子在归一化中抵消）"""
    if T <= 0 or sigma <= 0:
        return max(F - K, 0.0)
    d1 = (np.log(F / K) + 0.5 * sigma * sigma * T) / (sigma * np.sqrt(T))
    d2 = d1 - sigma * np.sqrt(T)
    return F * norm.cdf(d1) - K * norm.cdf(d2)


def rnd_from_sabr(F, alpha, beta, rho, nu, T, x_bound=X_BOUND, n=N_GRID):
    """
    在 log-moneyness 空间 x=ln(K/F) 上，用 SABR 平滑 IV 做 Breeden-Litzenberger
    二阶导，提取风险中性分布 (RND) 概率密度。返回 {x, pdf, F, K, atm_iv}。

    平移不变性：因在 x 空间作业，纯 spot/forward 移动不改变 RND 形状，
    只有 skew/vol/kurtosis 变化才会被 W1 捕捉（符合设计）。
    """
    if F <= 0 or T <= 0:
        return None
    x = np.linspace(-x_bound, x_bound, n)
    K = F * np.exp(x)
    # 每层 IV 用 SABR 公式算出（已是平滑 surface，无原始噪声）
    ivs = np.array([
        max(1e-4, min(5.0, sabr_iv(F, kk, T, alpha, beta, rho, nu)))
        for kk in K
    ])
    C = np.array([_bs_call(F, kk, T, s) for kk, s in zip(K, ivs)])
    dx = x[1] - x[0]
    Cx = np.gradient(C, dx)
    Cxx = np.gradient(Cx, dx)
    pdf_raw = np.exp(-x) * (Cxx - Cx)     # ∝ d²C/dK²（差常数因子）
    pdf_raw = np.clip(pdf_raw, 0, None)
    area = _trapz(pdf_raw, x)
    if area <= 0 or not np.isfinite(area):
        return None
    pdf = pdf_raw / area
    atm_iv = sabr_iv(F, F, T, alpha, beta, rho, nu)
    return {"x": x, "pdf": pdf, "F": F, "K": K, "atm_iv": atm_iv}


def wasserstein_1d(p, q, x):
    """一维 Wasserstein 闭式：两 CDF 的 L1 距离（同一支撑网格）"""
    dx = x[1] - x[0]
    cdf_p = np.zeros_like(p)
    cdf_q = np.zeros_like(q)
    cdf_p[1:] = np.cumsum((p[:-1] + p[1:]) * 0.5 * dx)
    cdf_q[1:] = np.cumsum((q[:-1] + q[1:]) * 0.5 * dx)
    return float(_trapz(np.abs(cdf_p - cdf_q), x))


def classify_jump_trend(w1_by_lag, threshold=2.5):
    """ratio = W1(t,t-7)/W1(t,t-1)。≈1 → jump（信）；≫1 → trend（丢）。"""
    w1_1 = w1_by_lag.get(1)
    w1_7 = w1_by_lag.get(7)
    if w1_1 is None or w1_7 is None:
        return "n/a", None
    ratio = w1_7 / w1_1 if w1_1 > 0 else float("inf")
    typ = "trend" if ratio > threshold else "jump"
    return typ, ratio


def _forward_for(slices, cur, dte):
    best = None
    for s in slices:
        if s.currency != cur:
            continue
        if best is None or abs(s.dte - dte) < abs(best[0] - dte):
            best = (s.dte, s.forward)
    return best[1] if best else 0.0


def build_sabr_items(sabr_params, slices):
    """按币种聚合 (dte, SabrParams, forward) 列表，按 dte 升序。"""
    items = {}
    for p in sabr_params.values():
        cur = p.currency
        F = _forward_for(slices, cur, p.dte)
        items.setdefault(cur, []).append((p.dte, p, F))
    for cur in items:
        items[cur].sort(key=lambda t: t[0])
    return items


def _interp_sabr_for_tenor(items, target_dte):
    """在相邻到期间对 SABR 参数做 constant-maturity 线性插值到正好 target_dte。"""
    if not items:
        return None
    dtes = [it[0] for it in items]
    if target_dte <= dtes[0]:
        it = items[0]
        return (it[1].alpha, it[1].beta, it[1].rho, it[1].nu, it[2])
    if target_dte >= dtes[-1]:
        it = items[-1]
        return (it[1].alpha, it[1].beta, it[1].rho, it[1].nu, it[2])
    for i in range(len(items) - 1):
        d0, p0, f0 = items[i]
        d1, p1, f1 = items[i + 1]
        if d0 <= target_dte <= d1:
            w = (target_dte - d0) / (d1 - d0) if d1 != d0 else 0.0
            alpha = p0.alpha + w * (p1.alpha - p0.alpha)
            rho = p0.rho + w * (p1.rho - p0.rho)
            nu = p0.nu + w * (p1.nu - p0.nu)
            f = f0 + w * (f1 - f0)
            return (alpha, p0.beta, rho, nu, f)
    return None


def _snapshot_at_or_before(history_list, target_ts):
    best = None
    for h in history_list:
        if h["ts"] <= target_ts:
            if best is None or h["ts"] > best["ts"]:
                best = h
    return best


def _should_record(history_list, now, cfg):
    """是否记录一帧 RND 快照。

    snapshot_mode:
      hourly — 每小时 UTC 整点(分钟==0)存一帧，用于实时积累历史 surface。
                配合 min_snapshot_gap_sec(<3600) 防止同一整点窗口内重复记帧。
      daily  — 旧模式：指定 UTC 钟点跨日存一帧（snapshot_utc_hour 生效）。
    """
    if not history_list:
        return True
    last_ts = history_list[-1]["ts"]
    mode = cfg.get("snapshot_mode", "hourly")
    min_gap = int(cfg.get("min_snapshot_gap_sec", 3500))
    now_gm = time.gmtime(now)
    if mode == "hourly":
        on_hour = (now_gm.tm_min == 0)
        gap_ok = (now - last_ts) >= min_gap
        return on_hour and gap_ok
    # daily 模式（向后兼容）
    target_hour = int(cfg.get("snapshot_utc_hour", 0))
    last = time.gmtime(last_ts)
    crossed_hour = (now_gm.tm_hour == target_hour) and (last.tm_yday != now_gm.tm_yday)
    gap_ok = (now - last_ts) >= min_gap
    return crossed_hour or gap_ok


def _trim(history_list, max_days):
    if max_days is None:
        return
    cutoff = time.time() - max_days * 86400
    while history_list and history_list[0]["ts"] < cutoff:
        history_list.pop(0)


def _load_history(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_history(path, history):
    try:
        import os
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(history, f)
    except Exception as e:
        logger.warning(f"W1 历史落盘失败: {e}")


def evaluate_signal(w1_result, cfg):
    thr = float(cfg.get("trigger_w1_min", 0.02))
    triggered = []
    details = {}
    for cur, tenors in w1_result.items():
        t30 = tenors.get(30, {})
        t7 = tenors.get(7, {})
        w30 = (t30.get("w1") or {}).get(1)
        w7 = (t7.get("w1") or {}).get(1)
        j30 = t30.get("type") == "jump"
        j7 = t7.get("type") == "jump"
        ok = (w30 is not None and w30 > thr and j30) and (w7 is not None and w7 > thr and j7)
        details[cur] = {"w1_30": w30, "w1_7": w7, "jump_30": j30, "jump_7": j7, "ok": ok}
        if ok:
            triggered.append(cur)
    return {
        "triggered": len(triggered) > 0,
        "currencies": triggered,
        "details": details,
        "threshold": thr,
        "note": "占位固定阈值；M2 OOS 验证后改为样本分位阈值（walk-forward）",
    }


def compute_w1_state(web_state, cfg, now=None):
    """计算并写入 web_state["w1_state"]；记录 RND 历史快照。纯只读旁路。"""
    now = now or time.time()
    sabr_params = web_state.get("latest_sabr_params", {}) or {}
    slices = web_state.get("latest_slices", []) or []
    if not sabr_params or not slices:
        return {"status": "no_data", "updated_ts": now,
                "message": "SABR 参数或期权链快照尚未就绪"}

    by_cur = build_sabr_items(sabr_params, slices)
    tenors = cfg.get("tenors", [7, 30])
    lags = cfg.get("lags_days", [1, 3, 7])
    jump_thr = float(cfg.get("jump_ratio_threshold", 2.5))

    # 1) 实时计算当前 RND（不立即写历史，仅供展示 + 记录用）
    current = {}
    for cur, items in by_cur.items():
        for Td in tenors:
            res = _interp_sabr_for_tenor(items, Td)
            if not res:
                continue
            alpha, beta, rho, nu, F = res
            T = Td / 365.0
            rnd = rnd_from_sabr(F, alpha, beta, rho, nu, T)
            if rnd:
                current[(cur, Td)] = rnd

    # 2) 记录快照（按 tenor 各存一条）
    history = web_state.setdefault("w1_rnd_history", {})
    path = cfg.get("history_path", "data/w1_rnd_history.json")
    recorded = False
    for (cur, Td), rnd in current.items():
        hcur = history.setdefault(cur, {})
        hten = hcur.setdefault(str(Td), [])
        if _should_record(hten, now, cfg):
            hten.append({
                "ts": now,
                "x": rnd["x"].tolist(),
                "pdf": rnd["pdf"].tolist(),
                "F": rnd["F"],
            })
            _trim(hten, cfg.get("max_history_days", 120))
            recorded = True
    if recorded:
        _save_history(path, history)

    # 3) 基于已记录历史计算 W1（lag 以天为单位映射到历史快照）
    w1_result = {}
    for cur in by_cur:
        tenor_res = {}
        for Td in tenors:
            hten = history.get(cur, {}).get(str(Td), [])
            if len(hten) < 2:
                tenor_res[Td] = {"w1": None, "type": "n/a",
                                 "reason": "insufficient_history"}
                continue
            cur_snap = hten[-1]
            px = np.array(cur_snap["x"])
            ppdf = np.array(cur_snap["pdf"])
            w1_by_lag = {}
            for k in lags:
                target_ts = cur_snap["ts"] - k * 86400
                past = _snapshot_at_or_before(hten, target_ts)
                if past and past["ts"] < cur_snap["ts"]:
                    w1_by_lag[k] = wasserstein_1d(
                        ppdf, np.array(past["pdf"]), px)
            typ, ratio = classify_jump_trend(w1_by_lag, jump_thr)
            tenor_res[Td] = {
                "w1": {str(k): round(v, 6) for k, v in w1_by_lag.items()},
                "type": typ,
                "jump_ratio": (None if ratio is None else round(ratio, 3)),
            }
        w1_result[cur] = tenor_res

    # 4) 信号评估
    signal = evaluate_signal(w1_result, cfg)

    snap_count = {
        cur: {str(Td): len(history.get(cur, {}).get(str(Td), []))
              for Td in tenors}
        for cur in by_cur
    }

    # 5) 序列与对比（前端展示用）：W1 时间序列 + RND 形状对比（今 vs 昨）
    w1_series = _build_w1_series(history, by_cur, tenors)
    rnd_compare = _build_rnd_compare(history, by_cur, tenors)

    return {
        "status": "ok" if current else "no_data",
        "updated_ts": now,
        "updated_at": time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(now)) + " UTC",
        "tenors": {str(t): True for t in tenors},
        "current_rnd_meta": {
            f"{cur}_{Td}": {"F": round(rnd["F"], 1), "atm_iv": round(rnd["atm_iv"], 4)}
            for (cur, Td), rnd in current.items()
        },
        "w1": w1_result,
        "signal": signal["triggered"],
        "trigger": signal,
        "snapshot_count": snap_count,
        "w1_series": w1_series,
        "rnd_compare": rnd_compare,
        "note": "W1 信号正在积累历史数据，满 24 小时后显示",
    }


def _build_w1_series(history, by_cur, tenors, max_points=240):
    """供前端画折线：对每个币种/tenor，输出每帧相对上一帧(环比)的 W1 序列。

    序列点数为 history 最近 max_points 帧；W1 以相邻快照为基准(frame-to-frame)，
    从第 2 帧起即有值，不再要求满 1 天历史。语义=相邻快照间 RND 位移(日内微观漂移)。
    首帧无上一帧，w1=None（前端折线自动断点）。capped 防 O(n^2) 在超长历史上爆炸。
    """
    series = {}
    for cur in by_cur:
        tmap = {}
        for Td in tenors:
            hten = history.get(cur, {}).get(str(Td), [])
            if len(hten) < 2:
                tmap[str(Td)] = []
                continue
            frames = hten[-max_points:]
            pts = []
            for i in range(len(frames)):
                ts = frames[i]["ts"]
                if i == 0:
                    pts.append({"ts": ts, "w1": None})
                    continue
                past = frames[i - 1]
                w = wasserstein_1d(
                    np.array(frames[i]["pdf"]),
                    np.array(past["pdf"]),
                    np.array(frames[i]["x"]),
                )
                pts.append({"ts": ts, "w1": round(float(w), 6)})
            tmap[str(Td)] = pts
        series[cur] = tmap
    return series


def _build_rnd_compare(history, by_cur, tenors):
    """供前端画 RND 形状对比：当前帧 vs 上一帧(环比)的密度曲线。

    返回 {cur: {str(Td): {x, pdf_now, pdf_prev, F_now, F_prev, ts_now, ts_prev}}}。
    两帧 x 网格相同（固定网格）。仅要求 ≥2 帧（即最早在积累第 2 帧时即可出图），
    不再要求满 1 天对照，避免早期历史阶段对比图长期空白。
    """
    out = {}
    for cur in by_cur:
        tmap = {}
        for Td in tenors:
            hten = history.get(cur, {}).get(str(Td), [])
            if len(hten) < 2:
                continue
            cur_snap = hten[-1]
            prev = hten[-2]
            tmap[str(Td)] = {
                "x": cur_snap["x"],
                "pdf_now": cur_snap["pdf"],
                "pdf_prev": prev["pdf"],
                "F_now": cur_snap["F"],
                "F_prev": prev["F"],
                "ts_now": cur_snap["ts"],
                "ts_prev": prev["ts"],
            }
        if tmap:
            out[cur] = tmap
    return out


# ===== M2 增强：quantile 形式 W1/W2 + decile 分析 =====
# 对齐源头仓库（vol-surface-opt-trans）的分位函数形式，便于复现其 0.54 量级数字，
# 并补充 W2（对尾部形变更敏感）与 decile 单调性验证（比单点 corr 更扎实）。

def _quantile_fn(x, pdf, u_grid):
    """由密度估计分位函数 Q^{-1}(u)：在累积概率上线性插值回 x 网格。"""
    dx = x[1] - x[0]
    cdf = np.zeros_like(pdf)
    cdf[1:] = np.cumsum((pdf[:-1] + pdf[1:]) * 0.5 * dx)
    cdf = np.clip(cdf, 0.0, 1.0)
    return np.interp(u_grid, cdf, x)


def wasserstein_1d_quantile(p, q, x, u_lo=0.01, u_hi=0.99, n_u=99):
    """一维 Wasserstein（分位函数形式）：mean_u |Q_p^{-1}(u) - Q_q^{-1}(u)|。
    与 wasserstein_1d（CDF-L1 形式）在一维下数学等价，但显式截断尾部 0/1 极值，
    数值更稳。对齐源头仓库实现，用于复现其相关量级。
    p, q 需为已归一化（面积=1）的同支撑密度。"""
    u = np.linspace(u_lo, u_hi, n_u)
    Qp = _quantile_fn(x, p, u)
    Qq = _quantile_fn(x, q, u)
    return float(np.mean(np.abs(Qp - Qq)))


def wasserstein_2_quantile(p, q, x, u_lo=0.01, u_hi=0.99, n_u=99):
    """一维 Wasserstein-2（平方形式）：sqrt(mean_u (Q_p^{-1}(u)-Q_q^{-1}(u))^2)。
    对分布尾部形变比 W1 更敏感，可作 jump/trend 区分的补充特征。"""
    u = np.linspace(u_lo, u_hi, n_u)
    Qp = _quantile_fn(x, p, u)
    Qq = _quantile_fn(x, q, u)
    return float(np.sqrt(np.mean((Qp - Qq) ** 2)))


def decile_table(w1_vals, vrp_vals, n_deciles=10):
    """按 W1 升序分十档，返回每档平均 W1 与平均 VRP。
    若平均 VRP 随档位单调上升，说明 W1 对 VRP 是单调预测（信号有效性的强证据，
    比单点 corr 更抗偶然）。输出每档 decile/n/w1_mean/vrp_mean。"""
    w1_vals = np.asarray(w1_vals, dtype=float)
    vrp_vals = np.asarray(vrp_vals, dtype=float)
    n = len(w1_vals)
    if n < n_deciles:
        return []
    order = np.argsort(w1_vals)
    w1_s = w1_vals[order]
    vrp_s = vrp_vals[order]
    out = []
    for d in range(n_deciles):
        lo = (d * n) // n_deciles
        hi = ((d + 1) * n) // n_deciles if d < n_deciles - 1 else n
        if hi <= lo:
            continue
        out.append({
            "decile": d + 1,
            "n": int(hi - lo),
            "w1_mean": float(np.mean(w1_s[lo:hi])),
            "vrp_mean": float(np.mean(vrp_s[lo:hi])),
        })
    return out


def decile_monotonicity(deciles):
    """返回 decile 表中 vrp_mean 的单调上升强度（Spearman 相关 0~1，越高越单调）。"""
    if not deciles or len(deciles) < 3:
        return None
    y = np.array([d["vrp_mean"] for d in deciles])
    ranks = np.argsort(np.argsort(y)) + 1  # 自评秩
    # 与档位序（1..n）的 Spearman
    x = np.arange(1, len(y) + 1)
    if np.std(x) == 0 or np.std(ranks) == 0:
        return None
    return float(np.corrcoef(x, ranks)[0, 1])
