"""
期权天眼 — 策略持仓管理

持仓数据从 Deribit 交易所实时拉取（private/get_positions），
不依赖本地内存/文件记录。重启后自动恢复持仓展示。
"""
import logging
import time
from typing import Optional

from data.models import Signal, SabrParams, ExpirySlice

logger = logging.getLogger(__name__)


class Position:
    """单笔策略持仓——数据来自交易所，signal 元数据由本地信号执行时附加"""

    def __init__(self, ex_data: dict, signal: Signal = None):
        """ex_data: private/get_positions 返回的单条持仓"""
        self.instrument = ex_data.get("instrument_name", "")
        # 币种字段是 currency（direction 是 buy/sell/zero，不是币种）
        self.currency = ex_data.get("currency", "")
        if not self.currency or len(self.currency) > 5:
            parts = self.instrument.split("-")
            self.currency = parts[0] if len(parts) > 0 else "BTC"
        self.kind = ex_data.get("kind", ex_data.get("option_type", ""))

        # 交易所实时 Greeks
        self.size = ex_data.get("size", 0) or ex_data.get("amount", 0)
        self.direction_net = "buy" if self.size > 0 else "sell"
        self.mark_price = ex_data.get("mark_price", 0)
        self.delta = ex_data.get("delta", 0)
        self.gamma = ex_data.get("gamma", 0)
        self.vega = ex_data.get("vega", 0)
        self.theta = ex_data.get("theta", 0)
        self.iv = ex_data.get("mark_iv", 0)
        self.open_interest = ex_data.get("open_interest", 0)

        # 入场价和盈亏（交易所提供）
        self.avg_price = ex_data.get("average_price", 0)      # 入场均价
        self.total_pnl = ex_data.get("total_profit_loss", 0)  # 总盈亏（已实现+未实现）
        self.unrealized_pnl = ex_data.get("profit_loss", 0)    # 未实现盈亏
        self.realized_pnl = ex_data.get("realized_profit_loss", 0)

        # 策略元数据（信号执行时附加，交易所没有）
        self.signal_id = signal.id if signal else ""
        self.strategy_type = signal.strategy_type if signal else ""
        self.direction = signal.direction if signal else ""
        self.description = signal.description if signal else ""
        self.hedge_instrument = signal.hedge_instrument if signal else ""
        self.hedge_direction = signal.hedge_direction if signal else ""
        self.hedge_amount = signal.hedge_amount if signal else 0.0
        self.hedge_active = False
        self.executed_at = int(time.time())

        self.status = "open"
        self.last_update = int(time.time())

    def to_dict(self) -> dict:
        return {
            "signal_id": self.signal_id,
            "currency": self.currency,
            "strategy_type": self.strategy_type,
            "direction": self.direction,
            "description": self.description,
            "instrument": self.instrument,
            "kind": self.kind,
            "size": self.size,
            "avg_price": self.avg_price,
            "mark_price": self.mark_price,
            "delta": round(self.delta, 4),
            "gamma": round(self.gamma, 4),
            "vega": round(self.vega, 4),
            "theta": round(self.theta, 4),
            "iv": round(self.iv, 4),
            "unrealized_pnl": round(self.unrealized_pnl, 6),
            "realized_pnl": round(self.realized_pnl, 6),
            "total_pnl": round(self.total_pnl, 6),
            "hedge_active": self.hedge_active,
            "hedge_instrument": self.hedge_instrument,
            "hedge_amount": round(self.hedge_amount, 4),
            "status": self.status,
            "executed_at": self.executed_at,
            "last_update": self.last_update,
        }


class PositionManager:
    """持仓管理器——从交易所实时拉取，叠加本地信号元数据"""

    def __init__(self):
        self._signal_map = {}      # signal_id -> Signal（信号执行时保存的元数据）
        self._hedge_map = {}       # signal_id -> {"active": bool, "amount": float, "instrument": str}

    def add_position(self, signal: Signal):
        """记录信号元数据（交易所持仓由拉取时自动匹配）"""
        self._signal_map[signal.id] = signal
        self._hedge_map[signal.id] = {
            "active": False,
            "amount": signal.hedge_amount,
            "instrument": signal.hedge_instrument,
            "direction": signal.hedge_direction,
        }
        logger.info(f"记录信号元数据: {signal.id} {signal.currency} {signal.strategy_type}")

    def set_hedge_active(self, signal_id: str, active: bool = True):
        """标记格致对冲状态"""
        if signal_id in self._hedge_map:
            self._hedge_map[signal_id]["active"] = active

    async def refresh_from_exchange(self, trader) -> list:
        """从交易所缓存读取持仓（首次填充 + user.changes 推送增量更新，零轮询）"""
        positions = []
        if not trader:
            return positions
        try:
            raw = trader.get_cached_positions()
            for item in raw:
                if item.get("kind") not in ("option",):
                    continue
                # 过滤掉 size=0 或 direction=zero 的空持仓（已平仓的历史记录）
                if item.get("size", 0) == 0 or item.get("direction") == "zero":
                    continue
                pos = Position(item)
                # 匹配信号元数据
                for sid, sig in list(self._signal_map.items()):
                    for leg in sig.legs:
                        if leg.get("instrument") == pos.instrument:
                            pos.signal_id = sid
                            pos.strategy_type = sig.strategy_type
                            pos.direction = sig.direction
                            pos.description = sig.description
                            pos.hedge_instrument = sig.hedge_instrument
                            pos.hedge_direction = sig.hedge_direction
                            hm = self._hedge_map.get(sid, {})
                            pos.hedge_active = hm.get("active", False)
                            pos.hedge_amount = hm.get("amount", 0)
                            pos.executed_at = getattr(sig, "created_at", int(time.time()))
                            break
                    if pos.signal_id:
                        break
                positions.append(pos)
        except Exception as e:
            logger.warning(f"从缓存读取持仓失败: {e}")
        return positions

    def get_net_position(self, positions: list) -> dict:
        """汇总所有持仓的 Greeks"""
        total = {"delta": 0, "gamma": 0, "vega": 0, "theta": 0,
                 "bartlett": 0, "count": 0, "pnl": 0.0}
        for p in positions:
            if p.status == "open":
                total["delta"] += p.delta
                total["gamma"] += p.gamma
                total["vega"] += p.vega
                total["theta"] += p.theta
                total["pnl"] += p.unrealized_pnl
                total["count"] += 1
        return {k: round(v, 4) if k != "count" else v for k, v in total.items()}

    def close_position(self, signal_id: str):
        """本地清除信号元数据（交易所持仓由下次拉取自动反映）"""
        self._signal_map.pop(signal_id, None)
        self._hedge_map.pop(signal_id, None)
        logger.info(f"清除信号元数据: {signal_id}")
