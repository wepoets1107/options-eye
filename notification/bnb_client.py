"""
Binance USDⓈ-M Futures WebSocket 客户端
从 Binance 永续合约获取 BTCUSDT 实时成交、K线、盘口、Mark Price
"""
import asyncio
import json
import logging
import time
import statistics
from collections import deque
from typing import Optional

import websockets

logger = logging.getLogger(__name__)

# 仅订阅真实数据源：aggTrade（成交）、kline_1m（OHLCV）、markPrice（标记价）
# 不订阅 @bookTicker：它会用中间价涨跌伪造成交并覆盖真实成交量，污染评分
BINANCE_WS = "wss://fstream.binance.com/stream?streams=btcusdt@aggTrade/btcusdt@kline_1m/btcusdt@markPrice@1s"

SH_TZ = time.timezone if time.daylight else time.timezone


def now_ms() -> int:
    return int(time.time() * 1000)


def safe_float(x, default=0.0) -> float:
    try:
        if x is None:
            return default
        v = float(x)
        if v != v or v == float("inf") or v == float("-inf"):
            return default
        return v
    except (ValueError, TypeError):
        return default


def ema(values, span):
    if len(values) < span:
        return None
    alpha = 2 / (span + 1)
    e = values[-span]
    for v in values[-span + 1:]:
        e = alpha * v + (1 - alpha) * e
    return e


class RollingMarket:
    """滚动市场数据（基于 Binance 永续）"""

    def __init__(self):
        self.closes = deque(maxlen=240)
        self.highs = deque(maxlen=240)
        self.lows = deque(maxlen=240)
        self.volumes = deque(maxlen=240)
        self.trades = deque(maxlen=20000)
        self.bb_widths = deque(maxlen=1000)

        self.last_price = 0.0
        self.mark_price = 0.0
        self.index_price = 0.0  # 由 options-eye 的 deribit_ws 更新

    def add_trade(self, ts, price, qty, taker_side):
        self.last_price = price
        self.trades.append((ts, price, qty, taker_side))
        self._trim_trades()

    def add_kline(self, ts, close, high, low, volume):
        if not self.closes or self.closes[-1][0] != ts:
            self.closes.append((ts, close))
            self.highs.append((ts, high))
            self.lows.append((ts, low))
            self.volumes.append((ts, volume))
        else:
            self.closes[-1] = (ts, close)
            self.highs[-1] = (ts, high)
            self.lows[-1] = (ts, low)
            self.volumes[-1] = (ts, volume)
        self.last_price = close
        self._update_bb_width()

    def _trim_trades(self):
        cutoff = now_ms() - 2 * 60 * 60 * 1000
        while self.trades and self.trades[0][0] < cutoff:
            self.trades.popleft()

    def _update_bb_width(self):
        closes = [x[1] for x in self.closes]
        if len(closes) >= 20:
            window = closes[-20:]
            mid = statistics.mean(window)
            sd = statistics.pstdev(window)
            if mid > 0:
                self.bb_widths.append((4 * sd) / mid)

    def vwap(self, minutes=120):
        cutoff = now_ms() - minutes * 60 * 1000
        pv = q = 0.0
        for ts, price, qty, _ in self.trades:
            if ts >= cutoff:
                pv += price * qty
                q += qty
        return pv / q if q > 0 else None

    def returns(self, minutes):
        closes = list(self.closes)
        if len(closes) < minutes + 1:
            return 0.0
        c0 = closes[-minutes - 1][1]
        return (closes[-1][1] / c0 - 1) if c0 else 0.0

    def donchian(self, minutes=120):
        if len(self.highs) < minutes or len(self.lows) < minutes:
            return None, None
        return max(x[1] for x in list(self.highs)[-minutes:]), \
               min(x[1] for x in list(self.lows)[-minutes:])

    def volume_ratio(self, lookback=20):
        vols = [x[1] for x in self.volumes]
        if len(vols) < lookback + 1:
            return 1.0
        avg = statistics.mean(vols[-lookback - 1:-1]) or 1.0
        return vols[-1] / avg

    def bb_width_percentile(self):
        if len(self.bb_widths) < 100:
            return None
        cur = self.bb_widths[-1]
        arr = list(self.bb_widths)
        return sum(1 for x in arr if x <= cur) / len(arr)

    @property
    def price(self):
        return self.index_price or self.mark_price or self.last_price

    def taker_ratio_5m(self) -> float:
        """过去5分钟主动买卖占比"""
        cutoff = now_ms() - 5 * 60 * 1000
        buy = sell = 0.0
        for ts, _, qty, side in self.trades:
            if ts >= cutoff:
                if side > 0:
                    buy += qty
                else:
                    sell += qty
        total = buy + sell
        return buy / total if total > 0 else 0.5


class BinanceFuturesClient:
    """Binance 永续 WebSocket 客户端"""

    def __init__(self, market: RollingMarket):
        self.market = market

    async def run(self):
        backoff = 30
        while True:
            try:
                async with websockets.connect(BINANCE_WS, ping_interval=20, ping_timeout=20) as ws:
                    logger.info("Binance Futures WS connected")
                    backoff = 30
                    async for raw in ws:
                        msg = json.loads(raw)
                        stream = msg.get("stream", "")
                        data = msg.get("data", {})
                        if stream.endswith("@aggTrade"):
                            p = safe_float(data.get("p"))
                            q = safe_float(data.get("q"))
                            ts = int(data.get("T") or data.get("E") or now_ms())
                            side = -1 if data.get("m") else 1
                            if p > 0 and q > 0:
                                self.market.add_trade(ts, p, q, side)
                        elif "@kline_1m" in stream:
                            k = data.get("k", {})
                            ts = int(k.get("t") or data.get("E") or now_ms())
                            self.market.add_kline(ts,
                                                  safe_float(k.get("c")),
                                                  safe_float(k.get("h")),
                                                  safe_float(k.get("l")),
                                                  safe_float(k.get("v")))
                        elif stream.endswith("@markPrice@1s"):
                            self.market.mark_price = safe_float(data.get("p"))
            except Exception as e:
                logger.warning(f"Binance WS error: {e}, reconnect in {backoff}s")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 300)
