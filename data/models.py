"""
期权天眼 — 数据模型结构
"""
from dataclasses import dataclass, field
from typing import Optional
from datetime import datetime


@dataclass
class OptionContract:
    """单个期权合约快照"""
    instrument: str               # BTC-31JUL26-85000-C
    currency: str                 # BTC / ETH
    kind: str                     # call / put
    strike: float
    expiration: int               # Unix timestamp (秒)
    dte: int                      # 距离到期天数
    mark_iv: float                # mark IV (小数, 如 0.452)
    bid_iv: Optional[float]       # bid IV
    ask_iv: Optional[float]       # ask IV
    mark_price: float             # mark 价格 (币本位)
    bid_price: float
    ask_price: float
    delta: float                  # BS Delta
    gamma: float
    vega: float
    theta: float
    open_interest: int
    volume_24h: int
    underlying_price: float       # 当前指数价格
    timestamp: int                # 数据时间戳


@dataclass
class ExpirySlice:
    """同一到期日的期权切片"""
    expiration: int
    dte: int
    forward: float                # 远期价格
    atm_iv: float                 # ATM IV
    currency: str = "BTC"          # BTC / ETH
    calls: list[OptionContract] = field(default_factory=list)
    puts: list[OptionContract] = field(default_factory=list)


@dataclass
class SabrParams:
    """SABR 校准参数（单个到期日）"""
    expiration: int
    dte: int
    alpha: float
    beta: float                   # 固定值 0.7
    rho: float
    nu: float
    currency: str = "BTC"          # BTC / ETH
    rmse: float = 0.0
    calibrated_at: int = 0
    converged: bool = True


@dataclass
class IVDeviation:
    """IV 偏差信号"""
    instrument: str
    currency: str
    kind: str                     # call / put
    strike: float
    expiration: int
    dte: int
    market_iv: float
    sabr_expected_iv: float
    deviation_pt: float           # 偏差（百分点）
    z_score: float                # 标准化偏差
    delta: float
    bid_iv: float
    ask_iv: float
    spread_filter_pass: bool      # 偏差是否大于 bid-ask spread
    oi_filter_pass: bool
    timestamp: int


@dataclass
class Signal:
    """策略信号"""
    id: str
    currency: str
    strategy_type: str            # straddle / strangle / risk_reversal / butterfly
    direction: str                # long / short
    confidence: str               # high / medium / low
    description: str
    legs: list[dict]              # [{instrument, direction, amount}, ...]
    hedge_instrument: str         # 对冲合约
    hedge_direction: str
    hedge_amount: float
    expected_premium: float       # 预计权利金收入
    estimated_delta: float        # 估计的初始 Delta
    deviations: list[IVDeviation]
    created_at: int
    status: str = "pending"       # pending / confirmed / executed / ignored / failed
