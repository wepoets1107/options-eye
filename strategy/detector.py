"""
期权天眼 — 偏差检测 + 策略匹配引擎
"""
import hashlib
import logging
import time
import numpy as np
from typing import Optional

from data.models import (
    OptionContract, ExpirySlice, SabrParams,
    IVDeviation, Signal
)
from sabr.calibrator import sabr_iv, expected_iv

logger = logging.getLogger(__name__)


def _stable_signal_id(currency: str, expiration: int, strategy_type: str, legs: list[dict]) -> str:
    """基于策略内容生成稳定 id（同一到期日+同类型+同腿组合 → 同 id），
    保证主循环多轮重建信号时 id 不变，确认/忽略状态不丢失。"""
    leg_key = "_".join(sorted(f"{l['instrument']}_{l['direction']}_{l.get('amount',1)}" for l in legs))
    raw = f"{currency}_{expiration}_{strategy_type}_{leg_key}"
    return hashlib.md5(raw.encode()).hexdigest()[:12]


def detect_deviations(
    slices: list[ExpirySlice],
    sabr_params: dict[str, SabrParams],
    z_threshold: float = 2.0,
    delta_min: float = 0.05,
    delta_max: float = 0.25,
    min_oi: int = 10
) -> list[IVDeviation]:
    deviations = []
    now = time.time()
    for slice_ in slices:
        sabr = sabr_params.get(f"{slice_.currency}_{slice_.expiration}")
        if not sabr:
            continue
        t_year = slice_.dte / 365.0
        f = slice_.forward
        if not f or f <= 0:
            continue
        all_devs = []
        contracts = slice_.calls + slice_.puts
        for c in contracts:
            if not (delta_min <= abs(c.delta) <= delta_max):
                continue
            if c.mark_iv <= 0:
                continue
            try:
                exp_iv = expected_iv(f, c.strike, t_year, sabr)
                if exp_iv <= 0:
                    continue
                dev_pt = (c.mark_iv - exp_iv) * 100
                all_devs.append(dev_pt)
            except:
                continue
        if len(all_devs) < 3:
            continue
        all_arr = np.array(all_devs)
        sigma = max(np.std(all_arr, ddof=1), 0.1) if len(all_arr) > 1 else 0.1
        for c in contracts:
            if not (delta_min <= abs(c.delta) <= delta_max):
                continue
            if c.mark_iv <= 0 or c.open_interest < min_oi:
                continue
            try:
                exp_iv = expected_iv(f, c.strike, t_year, sabr)
                if exp_iv <= 0:
                    continue
                dev_pt = (c.mark_iv - exp_iv) * 100
                z = dev_pt / sigma if sigma > 0 else 0
                spread_ok = True
                if c.bid_iv and c.ask_iv:
                    iv_spread = (c.ask_iv - c.bid_iv) * 100
                    # 偏差达到点差一半即视为有效信号（原要求超过点差，过狠导致 71% 被过滤）
                    if 0 < iv_spread and abs(dev_pt) < 0.5 * iv_spread:
                        spread_ok = False
                deviations.append(IVDeviation(
                    instrument=c.instrument, currency=c.currency,
                    kind=c.kind, strike=c.strike, expiration=c.expiration,
                    dte=c.dte, market_iv=c.mark_iv, sabr_expected_iv=exp_iv,
                    deviation_pt=round(dev_pt, 2), z_score=round(z, 2),
                    delta=round(c.delta, 4),
                    bid_iv=c.bid_iv or 0, ask_iv=c.ask_iv or 0,
                    spread_filter_pass=spread_ok,
                    oi_filter_pass=c.open_interest >= min_oi,
                    timestamp=int(now)
                ))
            except:
                pass
    return deviations


def _classify_deviation_pattern(expiry_deviations, log_mid_strike, delta_min=0.05, delta_max=0.25):
    """分析单个到期日的偏差模式

    优先检曲率：两翼高+中间正常 → butterfly
    再检整体：所有合约同号偏高/偏低 → strangle
    再检偏斜：call/put 一侧高一侧低 → risk_reversal

    wing/near 分档阈值跟随 delta_min/delta_max 动态计算（中点*0.6），
    避免 delta_min 调大后 wing 样本过少。
    z_score 可能被系统性偏差的 sigma 压缩，故 overpriced/underpriced
    额外用绝对偏差均值（pt）做辅助判定。"""
    if not expiry_deviations:
        return {"pattern": "none", "confidence": "low", "z_avg": 0}
    calls = [d for d in expiry_deviations if d.kind == "call"]
    puts = [d for d in expiry_deviations if d.kind == "put"]
    if not calls or not puts:
        return {"pattern": "none", "confidence": "low", "z_avg": 0}

    # wing 阈值动态化：默认 (0.05+0.25)/2*0.6=0.09，覆盖原硬编码 0.15 的常见场景
    wing_thresh = max(0.05, (delta_min + delta_max) / 2 * 0.6)
    wing_calls = [c for c in calls if abs(c.delta) < wing_thresh]
    wing_puts = [p for p in puts if abs(p.delta) < wing_thresh]
    near_calls = [c for c in calls if abs(c.delta) >= wing_thresh]
    near_puts = [p for p in puts if abs(p.delta) >= wing_thresh]

    wing_call_z = float(np.mean([c.z_score for c in wing_calls])) if wing_calls else 0
    wing_put_z = float(np.mean([p.z_score for p in wing_puts])) if wing_puts else 0
    near_call_z = float(np.mean([c.z_score for c in near_calls])) if near_calls else 0
    near_put_z = float(np.mean([p.z_score for p in near_puts])) if near_puts else 0
    avg_call_z = float(np.mean([c.z_score for c in calls]))
    avg_put_z = float(np.mean([p.z_score for p in puts]))
    # 绝对偏差均值（pt），辅助判定系统性偏高/偏低（避免 sigma 压缩 z 导致漏检）
    avg_call_pt = float(np.mean([c.deviation_pt for c in calls]))
    avg_put_pt = float(np.mean([p.deviation_pt for p in puts]))

    high_conf = lambda z: abs(z) >= 2.0

    # 1. 曲率检测：两翼高/低 + 近翼正常（wing 阈值动态化）
    if wing_calls and wing_puts and (near_calls or near_puts):
        if wing_call_z > 0.8 and wing_put_z > 0.8 and abs(near_call_z) < 0.8 and abs(near_put_z) < 0.8:
            return {"pattern": "convex", "confidence": "high" if (high_conf(wing_call_z) and high_conf(wing_put_z)) else "medium", "z_avg": (wing_call_z + wing_put_z) / 2}
        if wing_call_z < -0.8 and wing_put_z < -0.8 and abs(near_call_z) < 0.8 and abs(near_put_z) < 0.8:
            return {"pattern": "concave", "confidence": "high" if (high_conf(wing_call_z) and high_conf(wing_put_z)) else "medium", "z_avg": (wing_call_z + wing_put_z) / 2}

    # 2. 整体偏高/偏低：z 同号（阈值 1.2），或绝对偏差均值同号且 >=2pt（防 sigma 压缩漏检）
    z_ok = all(abs(z) >= 1.2 for z in [avg_call_z, avg_put_z]) and (avg_call_z > 0) == (avg_put_z > 0)
    pt_ok = (avg_call_pt >= 2 and avg_put_pt >= 2 and (avg_call_pt > 0) == (avg_put_pt > 0)) or \
            (avg_call_pt <= -2 and avg_put_pt <= -2 and (avg_call_pt > 0) == (avg_put_pt > 0))
    if z_ok or pt_ok:
        z_avg = (avg_call_z + avg_put_z) / 2
        if z_avg > 0 or (avg_call_pt > 0 and avg_put_pt > 0):
            return {"pattern": "overpriced", "confidence": "high" if (high_conf(z_avg) or avg_call_pt >= 4) else "medium", "z_avg": z_avg}
        else:
            return {"pattern": "underpriced", "confidence": "high" if (high_conf(z_avg) or avg_call_pt <= -4) else "medium", "z_avg": z_avg}

    # 3. 偏斜检测：一侧显著高于另一侧（阈值 1.2，差值 1.0）
    #    包括正向（一侧偏高）和反向（一侧偏低、另一侧正常）
    if avg_put_z > 1.2 and avg_call_z < avg_put_z - 1.0:
        return {"pattern": "skew_put_rich", "confidence": "high" if high_conf(avg_put_z) else "medium", "z_avg": avg_put_z}
    if avg_call_z > 1.2 and avg_put_z < avg_call_z - 1.0:
        return {"pattern": "skew_call_rich", "confidence": "high" if high_conf(avg_call_z) else "medium", "z_avg": avg_call_z}
    # 反向偏斜：一侧偏低、另一侧相对正常
    if avg_put_z < -1.2 and avg_call_z > avg_put_z + 1.0:
        return {"pattern": "skew_put_cheap", "confidence": "high" if high_conf(avg_put_z) else "medium", "z_avg": avg_put_z}
    if avg_call_z < -1.2 and avg_put_z > avg_call_z + 1.0:
        return {"pattern": "skew_call_cheap", "confidence": "high" if high_conf(avg_call_z) else "medium", "z_avg": avg_call_z}

    # 4. 兜底曲率（近翼数据不够时，阈值同步动态化）
    if wing_calls and wing_puts:
        if wing_call_z > 0.8 and wing_put_z > 0.8:
            return {"pattern": "convex", "confidence": "medium", "z_avg": (wing_call_z + wing_put_z) / 2}
        if wing_call_z < -0.8 and wing_put_z < -0.8:
            return {"pattern": "concave", "confidence": "medium", "z_avg": (wing_call_z + wing_put_z) / 2}

    return {"pattern": "none", "confidence": "low", "z_avg": 0}


def _finalize_signal(signal, slice_):
    """用真实 Greeks 计算净 delta、对冲量、预估权利金（覆盖临时字段）

    权利金口径 = 净现金流（卖出收权利金为正、买入付权利金为负），
    与 Position.entry_premium / current_premium 一致；pnl = entry - current。"""
    cmap = {c.instrument: c for c in (slice_.calls + slice_.puts)}
    net_delta = 0.0
    prem = 0.0
    for l in signal.legs:
        c = cmap.get(l["instrument"])
        if not c:
            continue
        # 权利金符号：卖=+收权利金，买=-付权利金
        cash_sign = 1 if l["direction"] == "sell" else -1
        prem += cash_sign * (c.mark_price or 0) * l["amount"]
        # Delta 符号：买=+（敞口与期权 delta 同向），卖=-（敞口与期权 delta 反向）
        # 例：买 Put（Put delta=-0.11）→ 敞口 delta = +1 * (-0.11) = -0.11 ✓
        # 例：卖 Call（Call delta=+0.16）→ 敞口 delta = -1 * (+0.16) = -0.16 ✓
        pos_sign = 1 if l["direction"] == "buy" else -1
        net_delta += pos_sign * c.delta
    signal.estimated_delta = round(net_delta, 4)
    signal.hedge_amount = round(abs(net_delta), 4)
    signal.hedge_direction = "long" if net_delta < 0 else "short"
    signal.expected_premium = round(prem, 6)


def generate_signals(deviations, slices, sabr_params, z_threshold=2.0, delta_min=0.05, delta_max=0.25):
    if not deviations:
        return []
    signals = []
    now = int(time.time())
    exp_groups = {}
    for d in deviations:
        key = f"{d.currency}_{d.expiration}"
        if key not in exp_groups:
            exp_groups[key] = []
        exp_groups[key].append(d)
    slice_map = {s.currency + "_" + str(s.expiration): s for s in slices}
    for exp_key, devs in exp_groups.items():
        slice_ = slice_map.get(exp_key)
        if not slice_:
            continue
        log_mid = slice_.forward
        pattern = _classify_deviation_pattern(devs, log_mid, delta_min=delta_min, delta_max=delta_max)
        if pattern["pattern"] == "none" or pattern["confidence"] == "low":
            continue
        # 配对用全部偏差（spread 仅作评级，不硬过滤）；
        # 至少要有 oi 达标的偏差才出信号（避免极低流动性噪音）
        trusted = [d for d in devs if d.oi_filter_pass]
        if not trusted:
            continue
        sabr = sabr_params.get(f"{slice_.currency}_{slice_.expiration}")
        # 传全部 devs 给 _build_signal，保证"有模式就能配对出腿"
        signal = _build_signal(pattern, devs, slice_, log_mid, now, sabr, delta_min, delta_max)
        if signal and (pattern["confidence"] == "high" or
                       (pattern["confidence"] == "medium" and abs(pattern["z_avg"]) >= 1.2)):
            _finalize_signal(signal, slice_)
            # 用稳定 id 覆盖 uuid，保证多轮重建信号时确认/忽略状态不丢失
            signal.id = _stable_signal_id(signal.currency, slice_.expiration, signal.strategy_type, signal.legs)
            signals.append(signal)
    return signals


def _build_signal(pattern, devs, slice_, log_mid, now, sabr=None, delta_min=0.05, delta_max=0.25):
    """根据偏差模式构建具体策略信号（用 SABR 算真实 Delta）"""
    pattern_type = pattern["pattern"]
    dte = slice_.dte
    currency = devs[0].currency if devs else "BTC"
    devs = [d for d in devs if d.currency == currency]

    def _fmt_delta(d, action):
        # 展示持仓 Delta（买卖方向 + 期权原生符号：call +, put -）
        # 买=与期权 delta 同向，卖=反向；例：买 Put(-0.19)→-0.19，卖 Call(+0.08)→-0.08
        signed = d.delta if action == "buy" else -d.delta
        return f"{signed:+.2f}"

    def _build_full_pool(kind):
        """该到期日全合约池（delta 在 [delta_min, delta_max]，不限 oi），
        用于配对时找对称腿；z_score 现算（中性腿 z≈0）。"""
        chain = slice_.calls if kind == "call" else slice_.puts
        base = []
        for c in (slice_.calls + slice_.puts):
            if not (delta_min <= abs(c.delta) <= delta_max):
                continue
            if c.mark_iv <= 0:
                continue
            try:
                e = expected_iv(log_mid, c.strike, slice_.dte / 365.0, sabr)
                if e > 0:
                    base.append((c.mark_iv - e) * 100)
            except Exception:
                pass
        sigma = max(float(np.std(base, ddof=1)), 0.1) if len(base) > 1 else 0.1
        pool = []
        for c in chain:
            if not (delta_min <= abs(c.delta) <= delta_max):
                continue
            if c.mark_iv <= 0:
                continue
            try:
                e = expected_iv(log_mid, c.strike, slice_.dte / 365.0, sabr)
                if e <= 0:
                    continue
                dev = (c.mark_iv - e) * 100
                z = dev / sigma if sigma > 0 else 0
                pool.append(IVDeviation(
                    instrument=c.instrument, currency=c.currency, kind=c.kind,
                    strike=c.strike, expiration=c.expiration, dte=c.dte,
                    market_iv=c.mark_iv, sabr_expected_iv=e,
                    deviation_pt=round(dev, 2), z_score=round(z, 2),
                    delta=round(c.delta, 4),
                    bid_iv=c.bid_iv or 0, ask_iv=c.ask_iv or 0,
                    spread_filter_pass=True, oi_filter_pass=c.open_interest >= 10,
                    timestamp=int(now)
                ))
            except Exception:
                pass
        return pool

    full_calls = _build_full_pool("call")
    full_puts = _build_full_pool("put")

    def _find_balanced_partner(primary, all_opposite):
        """找与主腿 Delta 对称的辅腿（不限制 Z 正负）"""
        target_abs_delta = abs(primary.delta)
        best = None
        best_score = 999
        for c in all_opposite:
            if c.instrument == primary.instrument:
                continue
            abs_diff = abs(abs(c.delta) - target_abs_delta)
            abs_z = abs(c.z_score)
            score = abs_diff * 0.7 + abs_z * 0.3
            if score < best_score:
                best_score = score
                best = c
        return best

    def _make_legs(primary, partner, pattern_tag):
        """根据主腿和辅腿的 Z 符号决定买卖方向和策略类型"""
        # 主腿：Z > 0 卖出（偏贵），Z < 0 买入（偏便宜）
        primary_action = "sell" if primary.z_score > 0 else "buy"
        partner_action = "sell" if partner.z_score > 0 else "buy"

        if primary_action == partner_action:
            # 同向 → strangle
            strat_type = "strangle"
        else:
            # 反向 → risk_reversal
            strat_type = "risk_reversal"

        # 确定 call 和 put 分别是谁
        call_leg = primary if primary.kind == "call" else partner
        put_leg = primary if primary.kind == "put" else partner

        return strat_type, call_leg, put_leg, primary_action, partner_action

    if pattern_type in ("overpriced", "underpriced"):
        all_calls = [d for d in devs if d.kind == "call"]
        all_puts = [d for d in devs if d.kind == "put"]
        if not all_calls or not all_puts:
            return None

        # 根据模式选主腿：overpriced 选 Z 最高的，underpriced 选 Z 最低的
        if pattern_type == "overpriced":
            best_call = sorted([d for d in all_calls if d.z_score > 0], key=lambda x: -x.z_score)
            best_put = sorted([d for d in all_puts if d.z_score > 0], key=lambda x: -x.z_score)
            primary_pool = (best_call[:1] if best_call else []) + (best_put[:1] if best_put else [])
        else:
            best_call = sorted([d for d in all_calls if d.z_score < 0], key=lambda x: x.z_score)
            best_put = sorted([d for d in all_puts if d.z_score < 0], key=lambda x: x.z_score)
            primary_pool = (best_call[:1] if best_call else []) + (best_put[:1] if best_put else [])

        if not primary_pool:
            return None

        # 从候选主腿中选 Z 绝对值最大的，然后找辅腿（全合约池配对）
        primary = max(primary_pool, key=lambda x: abs(x.z_score))
        opp_kind = "put" if primary.kind == "call" else "call"
        partner = _find_balanced_partner(primary, full_puts if opp_kind == "put" else full_calls)
        if not partner:
            return None

        strat_type, call_leg, put_leg, prim_act, part_act = _make_legs(primary, partner, pattern_type)
        direction = "short" if prim_act == "sell" else "long"

        desc_parts = [
            f"{'做空' if direction == 'short' else '做多'}{strat_type.replace('_',' ')} "
            f"{currency} {dte}d: "
            f"{prim_act} {primary.instrument} + {part_act} {partner.instrument} "
            f"(主腿Z={primary.z_score:.1f} 辅腿Z={partner.z_score:.1f}, "
            f"Call Δ={_fmt_delta(call_leg, prim_act)} Put Δ={_fmt_delta(put_leg, part_act)})"
        ]

        return Signal(
            id="",  # 由 generate_signals 用稳定 id 覆盖
            currency=currency,
            strategy_type=strat_type, direction=direction,
            confidence=pattern["confidence"],
            description="".join(desc_parts),
            legs=[
                {"instrument": primary.instrument, "direction": prim_act, "amount": 1},
                {"instrument": partner.instrument, "direction": part_act, "amount": 1},
            ],
            hedge_instrument=f"{currency}-PERPETUAL",
            hedge_direction="", hedge_amount=0,   # 由 _finalize_signal 用真实 Greeks 计算
            expected_premium=0, estimated_delta=0,
            deviations=devs, created_at=now
        )


    elif pattern_type == "skew_put_rich":
        # Put 偏贵 → 卖最贵的 put + 买 Delta 对称的 call（做多偏斜 / 牛市 risk_reversal）
        puts = sorted([d for d in devs if d.kind == "put"], key=lambda x: -x.z_score)
        calls = [d for d in devs if d.kind == "call"]
        if not puts or not calls:
            return None
        sell_leg = puts[0]
        buy_leg = _find_balanced_partner(sell_leg, full_calls)
        if not buy_leg:
            return None
        return Signal(
            id="", currency=currency,
            strategy_type="risk_reversal", direction="long",
            confidence=pattern["confidence"],
            description=(
                f"做多偏斜 {currency} {dte}d: "
                f"卖 {sell_leg.instrument} + 买 {buy_leg.instrument} "
                f"(Put Z={sell_leg.z_score:.1f} Δ={_fmt_delta(sell_leg, 'sell')}, "
                f"Call Z={buy_leg.z_score:.1f} Δ={_fmt_delta(buy_leg, 'buy')}, Put偏贵)"
            ),
            legs=[
                {"instrument": sell_leg.instrument, "direction": "sell", "amount": 1},
                {"instrument": buy_leg.instrument, "direction": "buy", "amount": 1},
            ],
            hedge_instrument=f"{currency}-PERPETUAL",
            hedge_direction="long", hedge_amount=0,
            expected_premium=0, estimated_delta=0,
            deviations=devs, created_at=now
        )

    elif pattern_type == "skew_call_rich":
        # Call 偏贵 → 卖最贵的 call + 买 Delta 对称的 put（做空偏斜 / 熊市 risk_reversal）
        calls = sorted([d for d in devs if d.kind == "call"], key=lambda x: -x.z_score)
        puts = [d for d in devs if d.kind == "put"]
        if not calls or not puts:
            return None
        sell_leg = calls[0]
        buy_leg = _find_balanced_partner(sell_leg, full_puts)
        if not buy_leg:
            return None
        return Signal(
            id="", currency=currency,
            strategy_type="risk_reversal", direction="short",
            confidence=pattern["confidence"],
            description=(
                f"做空偏斜 {currency} {dte}d: "
                f"卖 {sell_leg.instrument} + 买 {buy_leg.instrument} "
                f"(Call Z={sell_leg.z_score:.1f} Δ={_fmt_delta(sell_leg, 'sell')}, "
                f"Put Z={buy_leg.z_score:.1f} Δ={_fmt_delta(buy_leg, 'buy')}, Call偏贵)"
            ),
            legs=[
                {"instrument": sell_leg.instrument, "direction": "sell", "amount": 1},
                {"instrument": buy_leg.instrument, "direction": "buy", "amount": 1},
            ],
            hedge_instrument=f"{currency}-PERPETUAL",
            hedge_direction="short", hedge_amount=0,
            expected_premium=0, estimated_delta=0,
            deviations=devs, created_at=now
        )

    elif pattern_type == "skew_put_cheap":
        # Put 偏便宜 → 买最便宜的 put，卖 Delta 对称的 call（做空偏斜 / 熊市 risk_reversal）
        puts = sorted([d for d in devs if d.kind == "put"], key=lambda x: x.z_score)
        calls = [d for d in devs if d.kind == "call"]
        if not puts or not calls:
            return None
        buy_leg = puts[0]
        sell_leg = _find_balanced_partner(buy_leg, full_calls)
        if not sell_leg:
            return None
        return Signal(
            id="", currency=currency,
            strategy_type="risk_reversal", direction="short",
            confidence=pattern["confidence"],
            description=(
                f"做空偏斜 {currency} {dte}d: "
                f"买 {buy_leg.instrument} + 卖 {sell_leg.instrument} "
                f"(Put Z={buy_leg.z_score:.1f} Δ={_fmt_delta(buy_leg, 'buy')}, "
                f"Call Z={sell_leg.z_score:.1f} Δ={_fmt_delta(sell_leg, 'sell')}, Put偏低)"
            ),
            legs=[
                {"instrument": buy_leg.instrument, "direction": "buy", "amount": 1},
                {"instrument": sell_leg.instrument, "direction": "sell", "amount": 1},
            ],
            hedge_instrument=f"{currency}-PERPETUAL",
            hedge_direction="long", hedge_amount=0,
            expected_premium=0, estimated_delta=0,
            deviations=devs, created_at=now
        )

    elif pattern_type == "skew_call_cheap":
        # Call 偏便宜 → 买最便宜的 call，卖 Delta 对称的 put（做多偏斜 / 牛市 risk_reversal）
        calls = sorted([d for d in devs if d.kind == "call"], key=lambda x: x.z_score)
        puts = [d for d in devs if d.kind == "put"]
        if not calls or not puts:
            return None
        buy_leg = calls[0]
        sell_leg = _find_balanced_partner(buy_leg, full_puts)
        if not sell_leg:
            return None
        return Signal(
            id="", currency=currency,
            strategy_type="risk_reversal", direction="long",
            confidence=pattern["confidence"],
            description=(
                f"做多偏斜 {currency} {dte}d: "
                f"买 {buy_leg.instrument} + 卖 {sell_leg.instrument} "
                f"(Call Z={buy_leg.z_score:.1f} Δ={_fmt_delta(buy_leg, 'buy')}, "
                f"Put Z={sell_leg.z_score:.1f} Δ={_fmt_delta(sell_leg, 'sell')}, Call偏低)"
            ),
            legs=[
                {"instrument": buy_leg.instrument, "direction": "buy", "amount": 1},
                {"instrument": sell_leg.instrument, "direction": "sell", "amount": 1},
            ],
            hedge_instrument=f"{currency}-PERPETUAL",
            hedge_direction="short", hedge_amount=0,
            expected_premium=0, estimated_delta=0,
            deviations=devs, created_at=now
        )

    elif pattern_type in ("convex", "concave"):
        # Butterfly: 两翼（OTM call + OTM put）+ body（ATM）
        # convex → Short Butterfly（卖两翼 + 买 body），赚取曲率回归
        # concave → Long Butterfly（买两翼 + 卖 body）
        wing_calls = sorted([d for d in devs if d.kind == "call" and abs(d.delta) < 0.15], key=lambda x: -x.z_score)
        wing_puts = sorted([d for d in devs if d.kind == "put" and abs(d.delta) < 0.15], key=lambda x: -x.z_score)
        if not wing_calls or not wing_puts:
            return None
        # 两翼：各取 Z 最高的合约
        left_wing = wing_puts[0]   # OTM Put
        right_wing = wing_calls[0] # OTM Call

        # body：从 slice_ 中找 Delta 最接近 0.5 的 ATM 合约
        atm_cands = [c for c in (slice_.calls or []) if 0.35 <= abs(c.delta) <= 0.55] + \
                    [p for p in (slice_.puts or []) if 0.35 <= abs(p.delta) <= 0.55]
        body = min(atm_cands, key=lambda x: abs(abs(x.delta) - 0.5)) if atm_cands else None
        if not body:
            # 没有 ATM 合约（极端行情），改用近翼
            near_calls = [d for d in devs if d.kind == "call" and abs(d.delta) >= 0.15]
            near_puts = [d for d in devs if d.kind == "put" and abs(d.delta) >= 0.15]
            body_cands = near_calls + near_puts
            body = max(body_cands, key=lambda x: abs(x.z_score)) if body_cands else None
        if not body:
            return None

        dir_ = "short" if pattern_type == "convex" else "long"
        # Short Butterfly: 卖两翼卖（高 IV）+ 买 body（正常 IV）
        wing_act = "sell" if dir_ == "short" else "buy"
        body_act = "buy" if dir_ == "short" else "sell"

        return Signal(
            id="", currency=currency,
            strategy_type="butterfly", direction=dir_,
            confidence=pattern["confidence"],
            description=(
                f"{'做空' if dir_ == 'short' else '做多'}曲率 {currency} {dte}d: "
                f"{wing_act} {left_wing.instrument} + {wing_act} {right_wing.instrument} "
                f"+ {body_act} 2x {body.instrument} "
                f"(左翼Z={left_wing.z_score:.1f} 右翼Z={right_wing.z_score:.1f} "
                f"body Δ={(body.delta if body_act == 'buy' else -body.delta):+.2f})"
            ),
            legs=[
                {"instrument": left_wing.instrument, "direction": wing_act, "amount": 1},
                {"instrument": right_wing.instrument, "direction": wing_act, "amount": 1},
                {"instrument": body.instrument, "direction": body_act, "amount": 2},
            ],
            hedge_instrument=f"{currency}-PERPETUAL",
            hedge_direction="long", hedge_amount=0,
            expected_premium=0, estimated_delta=0,
            deviations=devs, created_at=now
        )
    return None
