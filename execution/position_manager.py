"""
期权天眼 — 策略持仓管理 + Greeks 计算
"""
import logging
import time
from typing import Optional

from data.models import Signal, SabrParams, ExpirySlice
from sabr.calibrator import sabr_iv, _calc_bartlett_delta

logger = logging.getLogger(__name__)


class Position:
    """单笔策略持仓"""

    def __init__(self, signal: Signal):
        self.signal_id = signal.id
        self.currency = signal.currency
        self.strategy_type = signal.strategy_type
        self.direction = signal.direction
        self.description = signal.description
        self.legs = list(signal.legs)
        self.hedge_instrument = signal.hedge_instrument
        self.executed_at = int(time.time())
        self.status = "open"  # open / closed

        # 入场权利金（净，币本位）。buy 为负、sell 为正。
        self.entry_premium = float(getattr(signal, "expected_premium", 0.0) or 0.0)
        # 盈亏（由外部基于实时 mark_price 更新）
        self.current_premium = 0.0      # 当前平仓所需净权利金（币本位）
        self.pnl = 0.0                  # 未实现盈亏 = entry_premium - current_premium
        self.pnl_pct = 0.0              # 盈亏百分比

        # Greeks（由外部更新）
        self.net_delta = 0.0
        self.net_gamma = 0.0
        self.net_vega = 0.0
        self.net_theta = 0.0
        self.bartlett_delta = 0.0

        # 对冲状态
        self.hedge_amount = 0.0         # 已对冲的永续合约数量
        self.hedge_active = False       # 格致对冲是否启用
        self.hedge_strategy_id = ""     # 格致策略 ID
        self.last_update = 0

    def to_dict(self) -> dict:
        return {
            "signal_id": self.signal_id,
            "currency": self.currency,
            "strategy_type": self.strategy_type,
            "direction": self.direction,
            "description": self.description,
            "legs": self.legs,
            "net_delta": round(self.net_delta, 4),
            "net_gamma": round(self.net_gamma, 4),
            "net_vega": round(self.net_vega, 4),
            "net_theta": round(self.net_theta, 4),
            "bartlett_delta": round(self.bartlett_delta, 4),
            "entry_premium": round(self.entry_premium, 6),
            "current_premium": round(self.current_premium, 6),
            "pnl": round(self.pnl, 6),
            "pnl_pct": round(self.pnl_pct, 2),
            "hedge_amount": round(self.hedge_amount, 4),
            "hedge_active": self.hedge_active,
            "hedge_strategy_id": self.hedge_strategy_id,
            "status": self.status,
            "executed_at": self.executed_at,
            "last_update": self.last_update,
        }


class PositionManager:
    """持仓管理器"""

    def __init__(self):
        self.positions: dict[str, Position] = {}
        self._sabr_params: dict[int, SabrParams] = {}
        self._slices: list[ExpirySlice] = []

    def add_position(self, signal: Signal) -> str:
        """添加新持仓"""
        pos = Position(signal)
        self.positions[signal.id] = pos
        logger.info(f"持仓添加: {signal.id} {signal.description}")
        return signal.id

    def close_position(self, signal_id: str):
        """平仓"""
        if signal_id in self.positions:
            self.positions[signal_id].status = "closed"
            logger.info(f"持仓平仓: {signal_id}")

    def update_greeks(self, sabr_params: dict[str, SabrParams], slices: list[ExpirySlice]):
        """更新所有持仓的 Greeks（用 SABR 模型算）"""
        self._sabr_params = sabr_params
        self._slices = {f"{s.currency}_{s.expiration}": s for s in slices}

        for pos_id, pos in self.positions.items():
            if pos.status != "open":
                continue
            self._update_position_greeks(pos)

    def _update_position_greeks(self, pos: Position):
        """计算单笔持仓的 Greeks（真实 pa Greeks 优先，缺失回退 SABR）"""
        total_delta = 0.0
        total_gamma = 0.0
        total_vega = 0.0
        total_theta = 0.0
        total_bartlett = 0.0
        total_premium_current = 0.0     # 当前平仓净权利金（币本位）

        for leg in pos.legs:
            inst = leg.get("instrument", "")
            direction = leg.get("direction", "buy")
            amount = leg.get("amount", 1)
            side = 1 if direction == "buy" else -1

            try:
                parts = inst.split("-")
                if len(parts) < 4:
                    continue
                currency = parts[0]
                strike = float(parts[2])
                kind = "call" if parts[3].upper() == "C" else "put"

                # 找对应的到期日切片
                from datetime import datetime, timezone
                try:
                    exp_dt = datetime.strptime(parts[1], "%d%b%y").replace(tzinfo=timezone.utc)
                except Exception:
                    continue

                slice_key = f"{currency}_{int(exp_dt.timestamp())}"
                slice_ = self._slices.get(slice_key)
                if not slice_:
                    continue

                sabr = self._sabr_params.get(f"{slice_.currency}_{slice_.expiration}")
                if not sabr:
                    continue

                f = slice_.forward
                t = slice_.dte / 365.0
                if f <= 0 or t <= 0:
                    continue

                # SABR IV 和 BS Greeks（用于 Bartlett 与回退）
                iv = sabr_iv(f, strike, t, sabr.alpha, sabr.beta, sabr.rho, sabr.nu)
                if iv <= 0:
                    continue

                from numpy import log, sqrt
                from scipy.stats import norm
                d1 = (log(f / strike) + 0.5 * iv ** 2 * t) / (iv * sqrt(t))
                if kind == "call":
                    bs_delta = norm.cdf(d1)
                    bs_vega = f * norm.pdf(d1) * sqrt(t) / 100
                else:
                    bs_delta = norm.cdf(d1) - 1
                    bs_vega = f * norm.pdf(d1) * sqrt(t) / 100
                bs_gamma = norm.pdf(d1) / (f * iv * sqrt(t))
                bs_theta = (-f * norm.pdf(d1) * iv) / (2 * sqrt(t)) / 365

                # 优先用真实 Greeks（main_loop 注入的 pa delta/vega/theta），缺失回退 SABR
                real_c = next((c for c in (slice_.calls + slice_.puts) if c.instrument == inst), None)
                if real_c and abs(real_c.delta) > 1e-6:
                    leg_delta = real_c.delta
                    leg_vega = real_c.vega if abs(real_c.vega) > 1e-9 else bs_vega
                    leg_gamma = real_c.gamma if abs(real_c.gamma) > 1e-9 else bs_gamma
                    leg_theta = real_c.theta if abs(real_c.theta) > 1e-9 else bs_theta
                else:
                    leg_delta = bs_delta
                    leg_vega = bs_vega
                    leg_gamma = bs_gamma
                    leg_theta = bs_theta

                # Bartlett Delta（依赖 SABR 参数）
                bartlett = _calc_bartlett_delta(
                    f, strike, t, sabr.alpha, sabr.beta, sabr.rho, sabr.nu,
                    bs_delta, bs_vega
                )

                total_delta += leg_delta * side * amount
                total_gamma += leg_gamma * side * amount
                total_vega += leg_vega * side * amount
                total_theta += leg_theta * side * amount
                total_bartlett += bartlett * side * amount

                # 当前平仓净权利金（仅期权腿，不含永续对冲腿）
                if real_c and real_c.mark_price and real_c.mark_price > 0:
                    total_premium_current += real_c.mark_price * amount * side

            except Exception as e:
                logger.debug(f"Greeks calc error for {inst}: {e}")

        pos.net_delta = total_delta
        pos.net_gamma = total_gamma
        pos.net_vega = total_vega
        pos.net_theta = total_theta
        pos.bartlett_delta = total_bartlett
        pos.current_premium = total_premium_current
        if abs(pos.entry_premium) > 1e-9:
            pos.pnl = pos.entry_premium - total_premium_current
            pos.pnl_pct = pos.pnl / abs(pos.entry_premium) * 100.0
        else:
            pos.pnl = 0.0
            pos.pnl_pct = 0.0
        pos.last_update = int(time.time())

    def get_net_position(self) -> dict:
        """获取所有持仓汇总"""
        total = {"delta": 0, "gamma": 0, "vega": 0, "theta": 0, "bartlett": 0, "pnl": 0.0, "count": 0}
        for p in self.positions.values():
            if p.status == "open":
                total["delta"] += p.net_delta
                total["gamma"] += p.net_gamma
                total["vega"] += p.net_vega
                total["theta"] += p.net_theta
                total["bartlett"] += p.bartlett_delta
                total["pnl"] += p.pnl
                total["count"] += 1
        return {k: round(v, 4) if k not in ("count", "pnl") else round(v, 6) for k, v in total.items()}

    def get_all_positions(self) -> list[dict]:
        return [p.to_dict() for p in self.positions.values() if p.status == "open"]
