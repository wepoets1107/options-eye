"""
期权天眼 — Deribit 下单执行层（需要认证）

当用户确认信号后，通过 Deribit 私有 API 下单执行
"""
import asyncio
import json
import logging
import time
from typing import Optional

import websockets

logger = logging.getLogger(__name__)


class DeribitTrader:
    """Deribit 交易客户端（私有 API，需要认证）"""

    def __init__(self, client_id: str, client_secret: str, testnet: bool = True):
        self.client_id = client_id
        self.client_secret = client_secret
        if testnet:
            self.url = "wss://test.deribit.com/ws/api/v2"
        else:
            self.url = "wss://www.deribit.com/ws/api/v2"

        self.ws: Optional[websockets.WebSocketClientProtocol] = None
        self._req_id = 0
        self._pending = {}
        self._lock = asyncio.Lock()
        self._token: Optional[str] = None
        self._token_expiry: float = 0
        # 持仓推送缓存（keyed by instrument_name），由 user.positions.* 订阅实时更新
        self._position_cache: dict[str, dict] = {}
        self._position_lock = asyncio.Lock()

    async def connect(self):
        """建立连接、认证、订阅用户数据推送、初始填充持仓缓存"""
        self.ws = await websockets.connect(self.url, ping_interval=30)
        asyncio.create_task(self._recv_loop())
        await self._authenticate()
        # 订阅持仓/订单/交易变更推送（实时，仅变动时推送）
        try:
            await self._send("private/subscribe", {
                "channels": ["user.changes.any.any.raw"]
            })
            logger.info("已订阅 user.changes.any.any.raw（持仓变更推送）")
        except Exception as e:
            logger.warning(f"订阅 user.changes 失败: {e}")
        # 初始填充持仓缓存（一次性，避免后续同步读取时阻塞事件循环）
        await self._fetch_positions()
        logger.info("DeribitTrader authenticated")

    async def _authenticate(self):
        """client_credentials 认证"""
        result = await self._send("public/auth", {
            "grant_type": "client_credentials",
            "client_id": self.client_id,
            "client_secret": self.client_secret
        })
        if "access_token" in result:
            self._token = result["access_token"]
            self._token_expiry = time.time() + result.get("expires_in", 3600) - 120
            logger.info("DeribitTrader auth OK")
        else:
            raise RuntimeError(f"Deribit auth failed: {result}")

    async def _ensure_auth(self):
        """确保 token 有效"""
        if not self._token or time.time() >= self._token_expiry:
            await self._authenticate()

    async def _send(self, method: str, params: dict = None) -> dict:
        """发送 JSON-RPC 请求"""
        async with self._lock:
            self._req_id += 1
            req_id = self._req_id
            future = asyncio.get_running_loop().create_future()
            self._pending[req_id] = future
            msg = {
                "jsonrpc": "2.0",
                "id": req_id,
                "method": method,
                "params": params or {}
            }
            # 对 private 方法自动加 access_token
            if method.startswith("private/") and self._token:
                if "access_token" not in msg.get("params", {}):
                    msg.setdefault("params", {})["access_token"] = self._token
            await self.ws.send(json.dumps(msg))

        result = await future
        return result.get("result", {})

    async def _recv_loop(self):
        """接收循环（处理请求响应 + subscription 推送）"""
        async for msg in self.ws:
            data = json.loads(msg)
            # 请求响应
            if "id" in data and data["id"] in self._pending:
                future = self._pending.pop(data["id"])
                error = data.get("error")
                if error:
                    future.set_exception(RuntimeError(str(error)))
                else:
                    future.set_result(data)
            # subscription 推送（user.changes / user.portfolio 等）
            elif data.get("method") == "subscription":
                params = data.get("params", {})
                channel = params.get("channel", "")
                payload = params.get("data", {})
                if channel.startswith("user.changes."):
                    async with self._position_lock:
                        if isinstance(payload, dict):
                            inst = payload.get("instrument_name", "")
                            if inst:
                                self._position_cache[inst] = payload
                        elif isinstance(payload, list):
                            for item in payload:
                                inst = item.get("instrument_name", "")
                                if inst:
                                    self._position_cache[inst] = item

    def get_cached_positions(self) -> list[dict]:
        """获取缓存中的持仓列表。
        初始数据在 connect() 时填充，后续由 user.changes 推送实时增量更新。零轮询。"""
        return list(self._position_cache.values())

    async def _fetch_positions(self):
        """填充持仓缓存（一次性请求，仅缓存为空时调用）"""
        for cur in ("BTC", "ETH"):
            try:
                result = await self._send("private/get_positions", {
                    "currency": cur, "kind": "option"
                })
                items = result if isinstance(result, list) else result.get("result", [])
                async with self._position_lock:
                    for item in items:
                        inst = item.get("instrument_name", "")
                        if inst:
                            self._position_cache[inst] = item
            except Exception as e:
                logger.warning(f"填充 {cur} 持仓缓存失败: {e}")

    async def get_order_state(self, order_id: str) -> dict:
        """查询订单状态"""
        await self._ensure_auth()
        return await self._send("private/get_order_state", {"order_id": order_id})

    async def place_order(
        self,
        instrument: str,
        direction: str,       # "buy" or "sell"
        amount: float,
        order_type: str = "market",
        label: str = "options-eye",
        price: Optional[float] = None,
        fill_timeout: float = 10.0
    ) -> dict:
        """下单并等待成交流水确认（处理部分成交/未成交）"""
        await self._ensure_auth()
        params = {
            "instrument_name": instrument,
            "amount": amount,
            "type": order_type,
            "label": label,
        }
        if direction == "buy":
            params["side"] = "buy"
        else:
            params["side"] = "sell"

        if order_type == "limit" and price:
            params["price"] = price

        method = "private/buy" if direction == "buy" else "private/sell"
        order = await self._send(method, params)
        if not isinstance(order, dict):
            return order

        order_id = order.get("order_id")
        state = order.get("order_state")

        # 市场单一般瞬间成交；若仍 open（限价/部分成交/未成），轮询确认成交
        if order_id and state in (None, "open", "unfilled", "partially_filled"):
            waited = 0.0
            while waited < fill_timeout:
                await asyncio.sleep(0.5)
                waited += 0.5
                try:
                    st = await self.get_order_state(order_id)
                except Exception as e:
                    logger.warning(f"get_order_state failed for {order_id}: {e}")
                    break
                if isinstance(st, dict):
                    state = st.get("order_state")
                    order = st
                    if state in ("filled", "rejected", "cancelled"):
                        break
            if state not in ("filled",):
                logger.warning(f"Order {order_id} not fully filled, final state={state}")
        return order

    async def execute_signal(self, signal: dict) -> dict:
        """
        执行策略信号（期权腿由本函数下单，对冲由格致 Trial Forge 自动管理）

        signal["legs"] = [{instrument, direction, amount}, ...]
        """
        results = {}

        # 1. 执行期权腿
        for leg in signal.get("legs", []):
            try:
                result = await self.place_order(
                    instrument=leg["instrument"],
                    direction=leg["direction"],
                    amount=leg["amount"],
                    order_type="market"
                )
                results[leg["instrument"]] = {
                    "status": "ok",
                    "order": result
                }
                logger.info(f"Order placed: {leg['direction']} {leg['amount']} {leg['instrument']}")
            except Exception as e:
                results[leg["instrument"]] = {
                    "status": "error",
                    "error": str(e)
                }
                logger.error(f"Order failed: {leg['instrument']}: {e}")

        # 2. 对冲由格致 Trial Forge 自动管理
        results["hedge"] = {"status": "delegated", "note": "格致 Trial Forge 自动对冲"}
        return results

    async def disconnect(self):
        if self.ws:
            await self.ws.close()
