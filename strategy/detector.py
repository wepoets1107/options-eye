"""
期权天眼 — 偏差检测 + 策略匹配引擎
"""
import logging
import time
import uuid
import numpy as np
from typing import Optional

from data.models import (
    OptionContract, ExpirySlice, SabrParams,
    IVDeviation, Signal
)
from sabr.calibrator import sabr_iv, expected_iv

logger = logging.getLogger(__name__)


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
                    if 0 < iv_spread < abs(dev_pt):
                        spread_ok = True
                    elif abs(dev_pt) < iv_spread:
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


def _classify_deviation_pattern(expiry_deviations, log_mid_strike):
    """分析单个到期日的偏差模式

    优先检曲率：两翼高+中间正常 → butterfly
    再检整体：所有合约同号偏高/偏低 → strangle
    再检偏斜：call/put 一侧高一侧低 → risk_reversal
    """
    if not expiry_deviations:
        return {"pattern": "none", "confidence": "low", "z_avg": 0}
    calls = [d for d in expiry_deviations if d.kind == "call"]
    puts = [d for d in expiry_deviations if d.kind == "put"]
    if not calls or not puts:
        return {"pattern": "none", "confidence": "low", "z_avg": 0}

    # 按 Delta 分档：
    # 深虚（翼）= Delta 0.05~0.15，浅虚（近翼/中间参考）= Delta 0.15~0.25
    wing_calls = [c for c in calls if abs(c.delta) < 0.15]
    wing_puts = [p for p in puts if abs(p.delta) < 0.15]
    near_calls = [c for c in calls if abs(c.delta) >= 0.15]
    near_puts = [p for p in puts if abs(p.delta) >= 0.15]

    wing_call_z = float(np.mean([c.z_score for c in wing_calls])) if wing_calls else 0
    wing_put_z = float(np.mean([p.z_score for p in wing_puts])) if wing_puts else 0
    near_call_z = float(np.mean([c.z_score for c in near_calls])) if near_calls else 0
    near_put_z = float(np.mean([p.z_score for p in near_puts])) if near_puts else 0
    avg_call_z = float(np.mean([c.z_score for c in calls]))
    avg_put_z = float(np.mean([p.z_score for p in puts]))

    high_conf = lambda z: abs(z) >= 2.0

    # 1. 曲率检测：两翼高/低 + 近翼正常
    if wing_calls and wing_puts and (near_calls or near_puts):
        if wing_call_z > 1.0 and wing_put_z > 1.0 and abs(near_call_z) < 0.8 and abs(near_put_z) < 0.8:
            return {"pattern": "convex", "confidence": "high" if (high_conf(wing_call_z) and high_conf(wing_put_z)) else "medium", "z_avg": (wing_call_z + wing_put_z) / 2}
        if wing_call_z < -1.0 and wing_put_z < -1.0 and abs(near_call_z) < 0.8 and abs(near_put_z) < 0.8:
            return {"pattern": "concave", "confidence": "high" if (high_conf(wing_call_z) and high_conf(wing_put_z)) else "medium", "z_avg": (wing_call_z + wing_put_z) / 2}

    # 2. 整体偏高/偏低：所有合约同号
    if all(abs(z) >= 1.5 for z in [avg_call_z, avg_put_z]) and (avg_call_z > 0) == (avg_put_z > 0):
        z_avg = (avg_call_z + avg_put_z) / 2
        if z_avg > 0:
            return {"pattern": "overpriced", "confidence": "high" if high_conf(z_avg) else "medium", "z_avg": z_avg}
        else:
            return {"pattern": "underpriced", "confidence": "high" if high_conf(z_avg) else "medium", "z_avg": z_avg}

    # 3. 偏斜检测：一侧显著高于另一侧
    if avg_put_z > 1.5 and avg_call_z < avg_put_z - 1.5:
        return {"pattern": "skew_put_rich", "confidence": "high" if high_conf(avg_put_z) else "medium", "z_avg": avg_put_z}
    if avg_call_z > 1.5 and avg_put_z < avg_call_z - 1.5:
        return {"pattern": "skew_call_rich", "confidence": "high" if high_conf(avg_call_z) else "medium", "z_avg": avg_call_z}

    # 4. 兜底曲率（近翼数据不够时）
    if wing_calls and wing_puts:
        if wing_call_z > 1.0 and wing_put_z > 1.0:
            return {"pattern": "convex", "confidence": "medium", "z_avg": (wing_call_z + wing_put_z) / 2}
        if wing_call_z < -1.0 and wing_put_z < -1.0:
            return {"pattern": "concave", "confidence": "medium", "z_avg": (wing_call_z + wing_put_z) / 2}

    return {"pattern": "none", "confidence": "low", "z_avg": 0}


def _finalize_signal(signal, slice_):
    """用真实 Greeks 计算净 delta、对冲量、预估权利金（覆盖临时字段）"""
    cmap = {c.instrument: c for c in (slice_.calls + slice_.puts)}
    net_delta = 0.0
    prem = 0.0
    for l in signal.legs:
        c = cmap.get(l["instrument"])
        if not c:
            continue
        sign = 1 if l["direction"] == "buy" else -1
        net_delta += sign * c.delta
        prem += sign * (c.mark_price or 0) * l["amount"]
    signal.estimated_delta = round(net_delta, 4)
    signal.hedge_amount = round(abs(net_delta), 4)
    signal.hedge_direction = "long" if net_delta < 0 else "short"
    signal.expected_premium = round(prem, 6)


def generate_signals(deviations, slices, sabr_params, z_threshold=2.0):
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
        pattern = _classify_deviation_pattern(devs, log_mid)
        if pattern["pattern"] == "none" or pattern["confidence"] == "low":
            continue
        valid_devs = [d for d in devs if d.spread_filter_pass and d.oi_filter_pass]
        if not valid_devs:
            continue
        sabr = sabr_params.get(f"{slice_.currency}_{slice_.expiration}")
        signal = _build_signal(pattern, valid_devs, slice_, log_mid, now, sabr)
        if signal and (pattern["confidence"] == "high" or
                       (pattern["confidence"] == "medium" and abs(pattern["z_avg"]) >= z_threshold)):
            _finalize_signal(signal, slice_)
            signals.append(signal)
    return signals


def _build_signal(pattern, devs, slice_, log_mid, now, sabr=None):
    """根据偏差模式构建具体策略信号（用 SABR 算真实 Delta）"""
    pattern_type = pattern["pattern"]
    dte = slice_.dte
    currency = devs[0].currency if devs else "BTC"
    devs = [d for d in devs if d.currency == currency]

    def _fmt_delta(d):
        return f"{abs(d.delta):.2f}"

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

        # 从候选主腿中选 Z 绝对值最大的，然后找辅腿
        primary = max(primary_pool, key=lambda x: abs(x.z_score))
        opposite = all_puts if primary.kind == "call" else all_calls
        partner = _find_balanced_partner(primary, opposite)
        if not partner:
            return None

        strat_type, call_leg, put_leg, prim_act, part_act = _make_legs(primary, partner, pattern_type)
        direction = "short" if prim_act == "sell" else "long"

        desc_parts = [
            f"{'做空' if direction == 'short' else '做多'}{strat_type.replace('_',' ')} "
            f"{currency} {dte}d: "
            f"{prim_act} {primary.instrument} + {part_act} {partner.instrument} "
            f"(主腿Z={primary.z_score:.1f} 辅腿Z={partner.z_score:.1f}, "
            f"Call Δ={_fmt_delta(call_leg)} Put Δ={_fmt_delta(put_leg)})"
        ]

        return Signal(
            id=uuid.uuid4().hex[:12], currency=currency,
            strategy_type=strat_type, direction=direction,
            confidence=pattern["confidence"],
            description="".join(desc_parts),
            legs=[
                {"instrument": primary.instrument, "direction": prim_act, "amount": 1},
                {"instrument": partner.instrument, "direction": part_act, "amount": 1},
            ],
            hedge_instrument=f"{currency}-PERPETUAL",
            hedge_direction="long" if call_leg.delta > 0.5 else "short",
            hedge_amount=abs(call_leg.delta + put_leg.delta - 1),
            expected_premium=0, estimated_delta=0,
            deviations=devs, created_at=now
        )


    elif pattern_type == "skew_put_rich":
        # Put 偏贵 → 主腿 = Z 最高的 put（卖），辅腿 = Delta 对称的 call（买）
        puts = sorted([d for d in devs if d.kind == "put"], key=lambda x: -x.z_score)
        calls = [d for d in devs if d.kind == "call"]
        if not puts or not calls:
            return None
        sell_leg = puts[0]
        buy_leg = _find_balanced_partner(sell_leg, calls)
        if not buy_leg:
            return None
        return Signal(
            id=uuid.uuid4().hex[:12], currency=currency,
            strategy_type="risk_reversal", direction="short",
            confidence=pattern["confidence"],
            description=(
                f"做空偏斜 {currency} {dte}d: "
                f"卖 {sell_leg.instrument} + 买 {buy_leg.instrument} "
                f"(Put Z={sell_leg.z_score:.1f} Δ={_fmt_delta(sell_leg)}, "
                f"Call Z={buy_leg.z_score:.1f} Δ={_fmt_delta(buy_leg)})"
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
        # Call 偏贵 → 主腿 = Z 最高的 call（卖），辅腿 = Delta 对称的 put（买）
        calls = sorted([d for d in devs if d.kind == "call"], key=lambda x: -x.z_score)
        puts = [d for d in devs if d.kind == "put"]
        if not calls or not puts:
            return None
        sell_leg = calls[0]
        buy_leg = _find_balanced_partner(sell_leg, puts)
        if not buy_leg:
            return None
        return Signal(
            id=uuid.uuid4().hex[:12], currency=currency,
            strategy_type="risk_reversal", direction="long",
            confidence=pattern["confidence"],
            description=(
                f"做多偏斜 {currency} {dte}d: "
                f"卖 {sell_leg.instrument} + 买 {buy_leg.instrument} "
                f"(Call Z={sell_leg.z_score:.1f} Δ={_fmt_delta(sell_leg)}, "
                f"Put Z={buy_leg.z_score:.1f} Δ={_fmt_delta(buy_leg)})"
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
            id=uuid.uuid4().hex[:12], currency=currency,
            strategy_type="butterfly", direction=dir_,
            confidence=pattern["confidence"],
            description=(
                f"{'做空' if dir_ == 'short' else '做多'}曲率 {currency} {dte}d: "
                f"{wing_act} {left_wing.instrument} + {wing_act} {right_wing.instrument} "
                f"+ {body_act} 2x {body.instrument} "
                f"(左翼Z={left_wing.z_score:.1f} 右翼Z={right_wing.z_score:.1f} "
                f"body Δ={abs(body.delta):.2f})"
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
