"""
VRP 反转信号 — M2 研究脚本 (walk-forward OOS 验证)

目的：在真实 crypto 历史上验证 "W1(t,t-k) 高 → 未来 realized variance 超 implied" 是否成立。
框架已完整，数据层做成可插拔 adapter：
  - mode=synthesize : 用合成 SPX 风格 RND 序列（含 jump/trend 场景）验证算法框架正确，
                      不依赖外部数据，作为 pipeline 自检。
  - mode=history    : 读取 options-eye 落库的 w1_rnd_history.json（实时积累或下载的真实 surface），
                      跑真实 OOS。需先有数据（见下方数据说明）。

M2 增强（对齐源头 vol-surface-opt-trans）：
  - W1 两种形式：CDF-L1 (wasserstein_1d) 与 分位函数 (wasserstein_1d_quantile)，互证一致性。
  - W2 (wasserstein_2_quantile)：对尾部形变更敏感，作 jump/trend 区分补充特征。
  - decile 单调性：按 W1 分十档看平均 VRP_reversal，比单点 corr 更抗偶然。
  - RV 日历对齐：7D tenor 严格用 7 日历天、30D 用 30 日历天（不混交易日数）。
  - walk-forward：前 70% 定阈值（90 分位），后 30% 测 OOS corr。

VRP_reversal 定义统一为 realized_var - implied_var（>0 即 realized 超 implied，即 VRP 反转发生）。
符合 "W1 高 → 未来 realized 超 implied" 的直觉；corr(W1, VRP_reversal) 期望为正。

数据说明（重要）：免费 Deribit API 仅提供实时 surface + 一个标量历史波动率序列，
拿不到历史 surface 快照（需 Deribit dbtool 或第三方付费）。真实 crypto OOS 须先解决数据源。
本脚本 synthesize 模式用于框架自检，不替代真实数据验证。
"""
import sys
import os
import json
import logging
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from strategy.w1_vrp import (
    wasserstein_1d, wasserstein_1d_quantile, wasserstein_2_quantile,
    decile_table, decile_monotonicity,
)

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("w1_research")

try:
    from numpy import trapezoid as _trapz
except ImportError:
    from numpy import trapz as _trapz

# matplotlib 可选：有则出散点 PNG，无则跳过（不阻塞框架）
try:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    HAVE_MPL = True
except Exception:
    HAVE_MPL = False

LAGS = (1, 3, 7)
X_BOUND = 0.9
N_GRID = 301


# ============================================================
# 数据 adapter
# ============================================================
def synthesize_rnd_series(n_days=600, seed=42, jump_prob=0.05):
    """合成 SPX 风格 RND 逐日演化，含 jump（左尾加厚+后续 realized 放大）与 trend（慢变 vol）。
    用于自检 pipeline：W1 应能在 jump 天升高，且 VRP_reversal 同步为正。"""
    rng = np.random.default_rng(seed)
    x = np.linspace(-X_BOUND, X_BOUND, N_GRID)
    series = []
    vol_state = 0.045  # 日度 vol 状态（慢变）
    F = 60000.0
    for i in range(n_days):
        vol_state *= np.exp(rng.normal(0, 0.012))
        vol_state = float(np.clip(vol_state, 0.02, 0.09))
        skew = 0.0
        jump = rng.random() < jump_prob
        if jump:
            skew = float(rng.uniform(0.4, 0.9))   # 左尾加厚（恐惧升温）
            vol_bump = 1.25
        else:
            vol_bump = 1.0
        sigma_x = vol_state * vol_bump * 1.4
        mu_x = -skew * sigma_x * 0.8
        pdf = np.exp(-0.5 * ((x - mu_x) / sigma_x) ** 2)
        pdf = np.clip(pdf, 0, None)
        area = _trapz(pdf, x)
        pdf = pdf / area
        # implied var（年化）：RND 二阶矩 ≈ 风险中性方差
        mean_x = _trapz(x * pdf, x)
        var_x = _trapz((x - mean_x) ** 2 * pdf, x)
        implied_var_annual = var_x * 365.0
        # 后续 7 日历天 realized（jump 后显著放大，trend 日中性偏小）
        realized_daily = vol_state * vol_bump * (1.0 + 1.8 * skew)
        realized_var_7d = (realized_daily ** 2) * 7.0
        implied_var_7d = implied_var_annual * (7.0 / 365.0)
        vrp_reversal = realized_var_7d - implied_var_7d
        series.append({
            "day": i, "ts": i * 86400, "x": x, "pdf": pdf, "F": F,
            "skew": skew, "jump": jump,
            "implied_var_7d": implied_var_7d,
            "realized_var_7d": realized_var_7d,
            "vrp_reversal": vrp_reversal,
        })
    return series


def load_rnd_history(path):
    """读 options-eye w1_rnd_history.json：{cur:{tenor_str:[{ts,x,pdf,F}]}}。
    转成按日对齐的 RND 序列（每币种每 tenor 独立）。"""
    with open(path, "r", encoding="utf-8") as f:
        raw = json.load(f)
    out = {}
    for cur, by_ten in raw.items():
        for ten_str, snaps in by_ten.items():
            seq = []
            for s in snaps:
                seq.append({
                    "day": s["ts"], "ts": s["ts"],
                    "x": np.array(s["x"]), "pdf": np.array(s["pdf"]),
                    "F": s.get("F", 0.0),
                })
            seq.sort(key=lambda r: r["ts"])
            out.setdefault(cur, {})[int(float(ten_str))] = seq
    return out


# ============================================================
# 信号计算
# ============================================================
def compute_w_series(seq, lags=LAGS):
    """对逐日 RND 序列计算 W1(CDF)/W1(quantile)/W2，按 lag 维度展开。"""
    n = len(seq)
    out = []
    for i in range(n):
        rec = {"day": seq[i]["day"], "ts": seq[i]["ts"], "cdf": {}, "q": {}, "w2": {}}
        x = seq[i]["x"]; p = seq[i]["pdf"]
        for k in lags:
            if i - k >= 0:
                q = seq[i - k]["pdf"]
                rec["cdf"][k] = wasserstein_1d(p, q, x)
                rec["q"][k] = wasserstein_1d_quantile(p, q, x)
                rec["w2"][k] = wasserstein_2_quantile(p, q, x)
        out.append(rec)
    return out


def analyze(seq, w_series, lags=LAGS):
    """主哨用 lag=1 的 W1(CDF)。输出 corr / decile / 单调性 / 三种形式一致性。"""
    w1 = np.array([r["cdf"].get(1) for r in w_series], dtype=float)
    w1q = np.array([r["q"].get(1) for r in w_series], dtype=float)
    w2 = np.array([r["w2"].get(1) for r in w_series], dtype=float)
    vrp = np.array([s["vrp_reversal"] for s in seq], dtype=float)
    mask = ~np.isnan(w1)
    if mask.sum() < 30:
        return None
    corr = float(np.corrcoef(w1[mask], vrp[mask])[0, 1])
    dec = decile_table(w1[mask], vrp[mask])
    mono = decile_monotonicity(dec)
    # 三种形式一致性（lag1 主哨）
    corr_cdf_q = float(np.corrcoef(w1[mask], w1q[mask])[0, 1])
    corr_cdf_w2 = float(np.corrcoef(w1[mask], w2[mask])[0, 1])
    return {
        "n": int(mask.sum()),
        "corr_w1_vrp": corr,
        "decile": dec,
        "monotonicity": mono,
        "corr_cdf_quantile": corr_cdf_q,
        "corr_cdf_w2": corr_cdf_w2,
    }


def walk_forward(seq, w_series, train_frac=0.7, lags=LAGS):
    """前 70% 定阈值（W1 lag1 的 90 分位），后 30% 测 OOS corr。"""
    w1 = np.array([r["cdf"].get(1) for r in w_series], dtype=float)
    vrp = np.array([s["vrp_reversal"] for s in seq], dtype=float)
    mask = ~np.isnan(w1)
    idx = np.where(mask)[0]
    cut = int(len(idx) * train_frac)
    train_idx = idx[:cut]
    oos_idx = idx[cut:]
    if len(oos_idx) < 30:
        return None
    thr = float(np.nanpercentile(w1[train_idx], 90))
    oos_corr = float(np.corrcoef(w1[oos_idx], vrp[oos_idx])[0, 1])
    # OOS 触发日的平均 VRP_reversal（证明信号方向对）
    trig = w1[oos_idx] > thr
    oos_trig_mean = float(np.mean(vrp[oos_idx][trig])) if trig.sum() > 0 else None
    oos_non_trig_mean = float(np.mean(vrp[oos_idx][~trig])) if (~trig).sum() > 0 else None
    return {
        "threshold": thr,
        "oos_corr": oos_corr,
        "oos_trig_n": int(trig.sum()),
        "oos_trig_vrp_mean": oos_trig_mean,
        "oos_nontrig_vrp_mean": oos_non_trig_mean,
    }


def backtest(seq, w_series, thr, lags=LAGS, theta_frac=0.5):
    """简化回测：W1(lag1)>thr 触发 → 买 7D ATM straddle 赌 realized 超 implied。
    pnl = VRP_reversal - theta_cost（theta 为 implied_var_7d 的固定比例，简化时间损耗）。
    注意：研究用"已实现 VRP_reversal"代理实际 pnl，未含手续费/滑点/对冲误差。"""
    rets = []
    for i, r in enumerate(w_series):
        w = r["cdf"].get(1)
        if w is None or np.isnan(w) or w <= thr:
            continue
        if i + 7 >= len(seq):
            continue
        theta_cost = theta_frac * seq[i]["implied_var_7d"]
        pnl = seq[i]["vrp_reversal"] - theta_cost
        rets.append(pnl)
    return np.array(rets) if rets else np.array([])


# ============================================================
# 输出
# ============================================================
def _print_report(name, res, wf, bt, theta_frac=0.5):
    print(f"\n{'='*60}\n[{name}]  M2 OOS 验证报告\n{'='*60}")
    if res is None:
        print("样本不足，跳过。")
        return
    print(f"有效样本数 n        : {res['n']}")
    print(f"corr(W1, VRP_rev)  : {res['corr_w1_vrp']:+.4f}")
    print(f"decile 单调性(Spear): {res['monotonicity']:+.4f}" if res['monotonicity'] is not None else "decile 单调性: n/a")
    print(f"corr(CDF, quantile): {res['corr_cdf_quantile']:+.4f}  (应≈1，验证两种形式一致)")
    print(f"corr(CDF, W2)       : {res['corr_cdf_w2']:+.4f}  (W2 对尾部更敏感)")
    print("\nDecile 表 (按 W1 升序分十档):")
    print(f"  {'档':>3} {'n':>4} {'W1_mean':>10} {'VRP_rev_mean':>14}")
    for d in res["decile"]:
        print(f"  {d['decile']:>3} {d['n']:>4} {d['w1_mean']:>10.5f} {d['vrp_mean']:>14.5f}")
    if wf:
        print(f"\nWalk-forward OOS:")
        print(f"  阈值(训练集90分位): {wf['threshold']:.5f}")
        print(f"  OOS corr          : {wf['oos_corr']:+.4f}")
        print(f"  OOS 触发日数      : {wf['oos_trig_n']}")
        print(f"  OOS 触发日均VRP_rev: {wf['oos_trig_vrp_mean']:+.5f}" if wf['oos_trig_vrp_mean'] is not None else "")
        print(f"  OOS 未触发日均VRP : {wf['oos_nontrig_vrp_mean']:+.5f}" if wf['oos_nontrig_vrp_mean'] is not None else "")
    if bt is not None and len(bt) > 0:
        print(f"\n简化回测 (straddle, theta={theta_frac}):")
        print(f"  交易次数 : {len(bt)}")
        print(f"  总 pnl   : {bt.sum():+.5f}")
        print(f"  胜率     : {float((bt>0).mean()):.2%}")
        print(f"  均值 pnl : {bt.mean():+.6f}")
    else:
        print("\n简化回测: 无触发交易")


def _scatter(name, seq, w_series, path):
    if not HAVE_MPL:
        return
    w1 = np.array([r["cdf"].get(1) for r in w_series], dtype=float)
    vrp = np.array([s["vrp_reversal"] for s in seq], dtype=float)
    mask = ~np.isnan(w1)
    plt.figure(figsize=(7, 5))
    sc = plt.scatter(w1[mask], vrp[mask], c=np.arange(mask.sum()), cmap="viridis", s=14)
    plt.xlabel("W1 (CDF-L1, lag1)")
    plt.ylabel("VRP_reversal (realized - implied, 7d)")
    plt.title(f"{name}: W1 vs VRP_reversal")
    plt.colorbar(sc, label="time")
    plt.tight_layout()
    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), path)
    plt.savefig(out, dpi=110)
    print(f"\n散点图已保存: {out}")


# ============================================================
# 入口
# ============================================================
def run_synthesize():
    print(">>> 模式: synthesize（合成数据自检，不依赖外部数据）")
    seq = synthesize_rnd_series(n_days=600, seed=123)
    print(f"合成序列天数: {len(seq)}  jump 天数: {sum(s['jump'] for s in seq)}")
    w_series = compute_w_series(seq)
    res = analyze(seq, w_series)
    wf = walk_forward(seq, w_series)
    bt = backtest(seq, w_series, wf["threshold"] if wf else 0.0)
    _print_report("SYNTHESIZE", res, wf, bt, theta_frac=0.5)
    _scatter("SYNTHESIZE", seq, w_series, "w1_research_synthesize.png")
    print("\n预期: corr>0 且 decile 单调上升（最高档 VRP_rev 最大），三种 W1 形式高度一致。")


def run_history(path):
    print(f">>> 模式: history（读取 {path}）")
    data = load_rnd_history(path)
    if not data:
        print("无数据，退出。")
        return
    for cur, by_ten in data.items():
        for ten, seq in by_ten.items():
            if len(seq) < 30:
                print(f"  {cur} {ten}D 样本不足({len(seq)})，跳过")
                continue
            w_series = compute_w_series(seq)
            # 真实数据需额外提供 VRP_reversal（由外部 RV 序列对齐），此处仅输出 W1 序列
            print(f"\n[{cur} {ten}D] 天数={len(seq)}")
            print("  W1(lag1) 描述:", end="")
            w1 = np.array([r["cdf"].get(1) for r in w_series], dtype=float)
            print(f" mean={np.nanmean(w1):.5f} median={np.nanmedian(w1):.5f} p90={np.nanpercentile(w1,90):.5f}")
            print("  (真实 VRP_reversal 需外部 realized variance 序列对齐后计算，见 README)")


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "synthesize"
    if mode == "synthesize":
        run_synthesize()
    elif mode == "history":
        path = sys.argv[2] if len(sys.argv) > 2 else "data/w1_rnd_history.json"
        run_history(path)
    else:
        print("用法: python w1_research.py [synthesize|history] [history_path]")


if __name__ == "__main__":
    main()
