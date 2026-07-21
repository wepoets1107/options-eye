"""
期权天眼 — Deribit WebSocket 客户端

设计原则（对照 Deribit API 规范）：
- 全推送、零轮询。期权链 IV 曲面 + Greeks 统一由 `ticker.{instrument}.{interval}`
  订阅频道推送（该频道自带 mark_iv / bid_iv / ask_iv / greeks / mark_price /
  open_interest，一个订阅替代原先的两处轮询）。
- Deribit 限制每连接最多 200 个订阅，并发连接数过多会触发服务端限流。
  因此全局 WebSocket 并发硬上限 3 个（控制连接 1 + ticker ≤2），ticker 连接默认 1 个（分批轮转覆盖全量）。
- 仅「获取合约列表」「指数价兜底」等极少数控制请求走主连接按需发送；
  运行期不轮询任何行情接口，避免触发限流/封禁。
- book.{currency}.summary 推送频道经实测不推送数据，故不作为数据源。
"""
import asyncio
import json
import logging
import time
from typing import Optional

import websockets

logger = logging.getLogger(__name__)

# 每连接最大订阅数（Deribit 硬上限 200，不留余量）
MAX_SUBS_PER_CONN = 200
# ticker 订阅连接硬上限（用户要求 ≤2）；当前实际启用见 TICKER_CONNS_ACTIVE
MAX_TICKER_CONNS = 2
# 全局 WebSocket 并发硬上限：控制连接(1) + ticker(≤2) 合计 ≤3，超限不再新建
MAX_WS_TOTAL = 3
# 实际启用的 ticker 连接数（必须 ≤ MAX_TICKER_CONNS）。1 个已稳定覆盖全量，
# 且握手频率最低、最不易触发 Deribit 429 限流；需更快覆盖时再上调至 2。
TICKER_CONNS_ACTIVE = 1
# 每批采集时长（秒）
BATCH_DWELL_SEC = 30
# ticker 推送间隔：Deribit 公共连接实测仅 100ms（及 raw 需认证）真正推送；
# 1s / 5s / 1m 在公共 API 上均不推送，故默认 100ms。
# illiquid 期权仅在变化时才推送，真实流量远小于频道数。
DEFAULT_TICKER_INTERVAL = "100ms"


class _TickerConn:
    """单个 ticker 订阅连接（仅订阅 ticker 频道，推送写入共享缓存）"""

    def __init__(self, url: str, idx: int, cache: dict, on_message):
        self.url = url
        self.idx = idx
        self.cache = cache                 # 共享 dict[inst] -> 归一化数据
        self.on_message = on_message       # callback(raw_data dict)
        self.ws = None
        self._pending: dict = {}
        self._req_id = 0
        self._lock = asyncio.Lock()
        self.subscribed: set = set()       # 当前已订阅的频道
        self.running = False
        self.retry_count = 0               # 重连尝试计数（供 recv 循环重连时续传退避）


class DeribitWS:
    """Deribit 公共 WebSocket（无需认证，全推送数据源）"""

    def __init__(self, url: str, ticker_interval: str = DEFAULT_TICKER_INTERVAL):
        self.url = url
        self.ticker_interval = ticker_interval

        # 控制连接（指数订阅 + 按需请求）
        self.ws = None
        self._req_id = 0
        self._pending: dict = {}
        self._lock = asyncio.Lock()
        self._running = False
        self._recv_task = None            # 控制连接 recv 协程引用
        self._supervisor_task = None      # 监督重连协程引用
        self._shutdown = False            # 关闭标志（disconnect 时置位）
        self._reconn_attempt = 0          # 重连尝试计数（用于退避）

        # 指数价格（deribit_price_index.{btc_usd,eth_usd} 推送）
        self.index_price: dict[str, float] = {}

        # ticker 推送缓存 —— 全系统唯一行情数据源（归一化后）
        self.ticker_cache: dict[str, dict] = {}
        self.last_ticker_ts: float = 0   # 最近一次收到 ticker 推送（看门狗基准）

        # ticker 订阅连接池
        self.ticker_conns: list[_TickerConn] = []

        # 已知期权合约列表（用于管理订阅）
        self.instruments: list[str] = []

        # 分批轮转采集
        self._all_ticker_channels: list[str] = []  # 所有合约频道
        self._ticker_batch_idx: int = 0            # 当前批次索引

        self.on_chain_update = None  # 预留回调（当前主循环按周期读缓存，未使用）

        # 轮转循环只启动一次（避免 contracts 刷新时重复 create_task）
        self._rotation_started = False
        # 看门狗协程引用（关闭时取消，避免协程泄漏）
        self._watchdog_task = None
        # 指数订阅状态（重连后需重新订阅，避免永久丢失）
        self._index_subscribed = False

    # ============ 连接管理 ============
    async def connect(self):
        """建链 + 初始化全部订阅（一次性，运行期不再轮询）"""
        await self._connect_main()
        self._supervisor_task = asyncio.create_task(self._supervisor())
        self._watchdog_task = asyncio.create_task(self._watchdog())
        # 拉合约列表 -> 建立 ticker 订阅（全推送起点）
        await self._init_chain()
        # 指数订阅
        await self.subscribe_index()
        self._index_subscribed = True
        # 一次性冷启动填充：保证首轮主循环立即有数据；之后由 ticker 推送维护
        try:
            await self.get_book_summary_by_currency("BTC")
            await self.get_book_summary_by_currency("ETH")
        except Exception as e:
            logger.warning(f"冷启动 book summary 失败（将等待 ticker 推送）: {e}")

    async def _connect_main(self):
        # 取消可能残留的旧 recv 协程，杜绝并发 recv
        # websockets 不允许同一连接被多个协程同时 recv，否则会报
        # "cannot call recv while another coroutine is already running" 并立即断开
        old = self._recv_task
        if old is not None and not old.done():
            old.cancel()
            try:
                await old
            except (asyncio.CancelledError, Exception):
                pass
        self.ws = await websockets.connect(self.url, ping_interval=30)
        self._running = True
        self._recv_task = asyncio.create_task(self._control_recv_loop())
        self._index_subscribed = False   # 新连接尚未订阅指数，待 _supervisor 补订阅
        logger.info("Deribit 控制连接已建立")

    async def _control_recv_loop(self):
        try:
            async for msg in self.ws:
                data = json.loads(msg)
                await self._handle_control(data)
        except asyncio.CancelledError:
            return  # 被重连逻辑取消，正常退出
        except Exception as e:
            logger.error(f"控制连接 recv 错误: {e}")
        finally:
            self._running = False
            # 清理悬空的 pending futures（连接断线后永远不会收到响应）
            for rid, fut in list(self._pending.items()):
                if not fut.done():
                    fut.set_exception(ConnectionError("控制连接已断开"))
                self._pending.pop(rid, None)
        # 注意：不在此处自重启，由 _supervisor 协程负责重连

    async def _supervisor(self):
        """监督控制连接 recv 协程：断开后无限重连。

        - 去掉原 10 次硬上限，改为无限重试直到恢复
        - 指数退避：普通失败 5→10→20→40→80→120s 封顶；
          HTTP 429 限流 60→120→240→300s 封顶（Deribit 对握手频率限流）
        - 重连成功后确保指数订阅恢复；订阅失败则下轮独立重试（不依赖 recv 断开，
          否则 recv 仍存活时会被卡在 sleep 分支、指数订阅永久丢失）
        """
        while not self._shutdown:
            # 1) 重连（recv 协程断开时）
            if self._recv_task is None or self._recv_task.done():
                try:
                    await self._connect_main()      # 内部置 _index_subscribed=False
                    self._reconn_attempt = 0
                except Exception as e:
                    self._reconn_attempt += 1
                    attempt = self._reconn_attempt
                    # 判定是否 HTTP 429 限流（握手频率限制）。websockets 握手限流抛
                    # InvalidStatusCode，其 str 含 "429" 且带 .status 属性，两种都判。
                    is_429 = "429" in str(e) or "Too Many" in str(e)
                    status = getattr(e, "status", None)
                    if status == 429:
                        is_429 = True
                    if is_429:
                        wait = min(60 * (2 ** min(attempt, 3)), 300)
                    else:
                        wait = min(5 * (2 ** min(attempt, 5)), 120)
                    logger.error(f"控制重连第{attempt}次失败，{wait}s 后重试: {e}")
                    await asyncio.sleep(wait)
                    continue
            # 2) 连接健康时，确保指数订阅成功（失败则下轮重试，不卡死）
            elif not self._index_subscribed:
                try:
                    await self.subscribe_index()
                    self._index_subscribed = True
                    logger.info("控制连接重连成功（指数订阅已恢复）")
                except Exception as e:
                    logger.warning(f"指数订阅失败，下轮重试: {e}")
                    await asyncio.sleep(5)
                    continue
            await asyncio.sleep(2)

    async def _handle_control(self, data: dict):
        # 请求响应
        if "id" in data and data["id"] in self._pending:
            self._pending.pop(data["id"]).set_result(data)
            return
        # 订阅推送（指数）
        if data.get("method") == "subscription":
            ch = data.get("params", {}).get("channel", "")
            if "index" in ch:
                d = data.get("params", {}).get("data", {})
                idx = d.get("index_name")
                price = d.get("index_price")
                if idx and price:
                    self.index_price[idx] = price

    async def _send(self, method: str, params: dict = None, timeout: float = 10.0) -> dict:
        """控制连接请求（带超时）"""
        if not self._running or not self.ws:
            raise ConnectionError("控制连接未就绪")
        async with self._lock:
            self._req_id += 1
            rid = self._req_id
            fut = asyncio.get_running_loop().create_future()
            self._pending[rid] = fut
            try:
                await self.ws.send(json.dumps({
                    "jsonrpc": "2.0", "id": rid,
                    "method": method, "params": params or {}
                }))
            except Exception as e:
                self._pending.pop(rid, None)
                if not fut.done():
                    fut.cancel()
                raise ConnectionError(f"发送失败: {e}")
        try:
            res = await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError:
            self._pending.pop(rid, None)
            raise TimeoutError(f"WS 请求超时: {method}")
        return res.get("result", {})

    async def disconnect(self):
        self._shutdown = True
        if self._supervisor_task is not None:
            self._supervisor_task.cancel()
        if self._watchdog_task is not None:
            self._watchdog_task.cancel()
        if self._recv_task is not None and not self._recv_task.done():
            self._recv_task.cancel()
        self._running = False
        if self.ws:
            await self.ws.close()
        for conn in self.ticker_conns:
            conn.running = False
            if conn.ws:
                await conn.ws.close()
        logger.info("Deribit WebSocket 已断开")

    # ============ 指数 ============
    async def subscribe_index(self):
        # 正确频道：deribit_price_index.btc_usd / deribit_price_index.eth_usd
        # （裸 deribit_price_index.raw 在实盘无效，订阅返回空）
        await self._send("public/subscribe", {
            "channels": ["deribit_price_index.btc_usd", "deribit_price_index.eth_usd"]
        })
        logger.info("已订阅指数价格 (btc_usd / eth_usd)")

    async def get_index_price(self, index: str) -> Optional[float]:
        # 仅读缓存，不发起网络请求（指数价走推送，REST兜底会触发WS超时）
        return self.index_price.get(index)

    # ============ 合约列表 ============
    async def get_instruments(self, currency: str) -> list[dict]:
        return await self._send("public/get_instruments", {
            "currency": currency, "kind": "option", "expired": False
        })

    async def _init_chain(self):
        insts: list[str] = []
        for cur in ("BTC", "ETH"):
            try:
                r = await self.get_instruments(cur)
                insts += [i["instrument_name"] for i in r]
            except Exception as e:
                logger.error(f"获取 {cur} 合约列表失败: {e}")
        self.instruments = insts
        logger.info(f"加载期权合约 {len(insts)} 个（BTC+ETH）")
        await self.manage_ticker_subscriptions(insts)

    # ============ ticker 订阅连接池 ============
    async def manage_ticker_subscriptions(self, instruments: list[str]):
        """单连接 + 分批轮转：任何时候只订阅 1 批（≤200 频道），每批停留 BATCH_DWELL_SEC 秒后切到下一批"""
        target = [f"ticker.{i}.{self.ticker_interval}" for i in instruments]
        self._all_ticker_channels = target
        total = len(target)
        n_batch = max(1, (total + MAX_SUBS_PER_CONN - 1) // MAX_SUBS_PER_CONN)
        logger.info(f"全量合约 {total} 个，分 {n_batch} 批采集（每批 ≤{MAX_SUBS_PER_CONN}，每批 {BATCH_DWELL_SEC}s）")

        # 确保 ticker 连接数不超过硬上限（清理多余），且全局 WS 并发 ≤ MAX_WS_TOTAL
        while len(self.ticker_conns) > MAX_TICKER_CONNS:
            extra = self.ticker_conns.pop()
            extra.running = False
            if extra.ws:
                asyncio.create_task(extra.ws.close())
        # 补足到实际启用数（受全局并发上限保护：控制1 + ticker 数 < MAX_WS_TOTAL）
        while len(self.ticker_conns) < TICKER_CONNS_ACTIVE:
            if 1 + len(self.ticker_conns) >= MAX_WS_TOTAL:
                logger.warning(f"已达全局 WS 并发上限 {MAX_WS_TOTAL}，停止新建 ticker 连接")
                break
            conn = _TickerConn(self.url, len(self.ticker_conns), self.ticker_cache, self._on_ticker)
            self.ticker_conns.append(conn)
            asyncio.create_task(self._ticker_connect(conn))
            await asyncio.sleep(0.2)

        # 订阅第一批，启动轮转（仅首次启动，刷新合约时不再重复 create_task）
        self._ticker_batch_idx = 0
        await self._switch_ticker_batch(0)
        if not self._rotation_started:
            self._rotation_started = True
            asyncio.create_task(self._ticker_rotation_loop())

    async def _switch_ticker_batch(self, batch_idx: int):
        """切换到指定批次"""
        total = len(self._all_ticker_channels)
        if total == 0:
            return
        start = batch_idx * MAX_SUBS_PER_CONN
        end = min(start + MAX_SUBS_PER_CONN, total)
        batch = self._all_ticker_channels[start:end]
        n_batch = max(1, (total + MAX_SUBS_PER_CONN - 1) // MAX_SUBS_PER_CONN)
        logger.info(f"切换 ticker 批次 {batch_idx + 1}/{n_batch}（{start}~{end}，共 {len(batch)} 频道）")
        conn = self.ticker_conns[0]
        await self._sync_conn(conn, batch)

    async def _ticker_rotation_loop(self):
        """后台轮转：每 BATCH_DWELL_SEC 切换到下一批"""
        try:
            while True:
                await asyncio.sleep(BATCH_DWELL_SEC)
                total = len(self._all_ticker_channels)
                if total == 0:
                    continue
                n_batch = max(1, (total + MAX_SUBS_PER_CONN - 1) // MAX_SUBS_PER_CONN)
                self._ticker_batch_idx = (self._ticker_batch_idx + 1) % n_batch
                await self._switch_ticker_batch(self._ticker_batch_idx)
        except Exception as e:
            logger.warning(f"ticker 轮转循环异常: {e}")
            # 不退出，由看门狗兜底

    async def _sync_conn(self, conn: _TickerConn, target_channels: list[str]):
        """对单个连接做 订阅/退订 diff"""
        target_set = set(target_channels)
        to_add = [c for c in target_channels if c not in conn.subscribed]
        to_del = [c for c in conn.subscribed if c not in target_set]
        if to_del:
            try:
                await self._tconn_send(conn, "public/unsubscribe", {"channels": to_del})
                await asyncio.sleep(0.5)  # 退订后等一会再订阅，减少限流风险
            except Exception as e:
                logger.warning(f"ticker#{conn.idx} unsubscribe 失败: {e}")
            conn.subscribed -= set(to_del)
        if to_add:
            try:
                await self._tconn_send(conn, "public/subscribe", {"channels": to_add})
                conn.subscribed |= set(to_add)
                logger.info(f"ticker 连接#{conn.idx} 当前订阅 {len(conn.subscribed)} 频道")
            except Exception as e:
                logger.warning(f"ticker#{conn.idx} subscribe 失败: {e}")
                # subscribe 失败后仍然更新 subscribed 为目标集，防止死循环重试
                conn.subscribed = target_set.copy()

    def _calc_backoff(self, attempt: int, is_429: bool) -> int:
        """重连退避：429 限流 60→120→240→300s 封顶；普通错误 5→10→...→120s 封顶"""
        if is_429:
            return min(60 * (2 ** min(attempt, 3)), 300)
        return min(5 * (2 ** min(attempt, 5)), 120)

    async def _ticker_connect(self, conn: _TickerConn, retry_count: int = 0):
        # 防重连协程累积：同一 conn 同时只跑 1 个重连协程，避免握手风暴触发 429
        if getattr(conn, "_reconnecting", False):
            return
        conn._reconnecting = True
        conn.retry_count = retry_count   # 记录当前尝试，供 recv 循环重连时续传退避
        try:
            conn.ws = await websockets.connect(self.url, ping_interval=30)
            # 重连后清空已订阅集合，强制下一轮轮转重新订阅（否则 subscribed
            # 与 target 一致会判定为空操作，连接空转 → 看门狗反复重连）
            conn.subscribed = set()
            conn.running = True
            conn._reconnecting = False
            logger.info(f"ticker 连接#{conn.idx} 已建立")
            await self._ticker_recv_loop(conn)
        except websockets.ConnectionClosed as e:
            conn.running = False
            conn._reconnecting = False
            is_429 = ("429" in str(e) or "Too Many" in str(e)
                       or getattr(e, "status", None) == 429)
            wait = self._calc_backoff(retry_count, is_429)
            logger.warning(f"ticker#{conn.idx} 连接关闭(429={is_429})，{wait}s 后重连: {e}")
            await asyncio.sleep(wait)
            asyncio.create_task(self._ticker_connect(conn, retry_count + 1))
        except Exception as e:
            conn.running = False
            conn._reconnecting = False
            is_429 = ("429" in str(e) or "Too Many" in str(e)
                       or getattr(e, "status", None) == 429)
            wait = self._calc_backoff(retry_count, is_429)
            logger.error(f"ticker 连接#{conn.idx} 错误(429={is_429})，{wait}s 后重连: {e}")
            await asyncio.sleep(wait)
            asyncio.create_task(self._ticker_connect(conn, retry_count + 1))

    async def _ticker_recv_loop(self, conn: _TickerConn):
        try:
            async for msg in conn.ws:
                data = json.loads(msg)
                if "id" in data and data["id"] in conn._pending:
                    conn._pending.pop(data["id"]).set_result(data)
                    continue
                if data.get("method") == "subscription":
                    ch = data.get("params", {}).get("channel", "")
                    if ch.startswith("ticker."):
                        self._on_ticker(data.get("params", {}).get("data", {}))
        except Exception as e:
            logger.warning(f"ticker 连接#{conn.idx} recv 错误: {e}")
            conn.running = False
            # 交给 _ticker_connect 统一退避重连（其入口有 _reconnecting 防累积）
            if not getattr(conn, "_reconnecting", False):
                asyncio.create_task(self._ticker_connect(conn, getattr(conn, "retry_count", 0)))

    async def _tconn_send(self, conn: _TickerConn, method: str, params: dict, timeout: float = 10.0) -> dict:
        for _ in range(50):
            if conn.ws is not None:
                break
            await asyncio.sleep(0.1)
        async with conn._lock:
            conn._req_id += 1
            rid = conn._req_id
            fut = asyncio.get_running_loop().create_future()
            conn._pending[rid] = fut
            await conn.ws.send(json.dumps({
                "jsonrpc": "2.0", "id": rid,
                "method": method, "params": params or {}
            }))
        try:
            res = await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError:
            conn._pending.pop(rid, None)
            raise TimeoutError(f"ticker 请求超时: {method}")
        return res.get("result", {})

    def _on_ticker(self, data: dict):
        norm = self._normalize(data)
        if norm:
            self.ticker_cache[norm["instrument_name"]] = norm
            self.last_ticker_ts = time.time()

    @staticmethod
    def _normalize(d: dict) -> Optional[dict]:
        """归一化 ticker / book_summary 原始数据为统一 schema

        兼容两种来源字段差异：
        - ticker: best_bid_price/best_ask_price, stats.volume
        - book_summary: bid_price/ask_price, 顶层 volume
        """
        inst = d.get("instrument_name")
        if not inst:
            return None
        stats = d.get("stats") or {}
        greeks = d.get("greeks") or {}
        return {
            "instrument_name": inst,
            "mark_iv": d.get("mark_iv"),
            "bid_iv": d.get("bid_iv"),
            "ask_iv": d.get("ask_iv"),
            "mark_price": d.get("mark_price"),
            "bid_price": d.get("best_bid_price") or d.get("bid_price"),
            "ask_price": d.get("best_ask_price") or d.get("ask_price"),
            "open_interest": d.get("open_interest", 0) or 0,
            "volume": stats.get("volume") or d.get("volume") or 0,
            "underlying_price": d.get("underlying_price"),
            "index_price": d.get("index_price"),
            "greeks": greeks,
            "ts": time.time(),
        }

    # ============ 冷启动 / 兜底：一次性拉取 ============
    async def get_book_summary_by_currency(self, currency: str, update_cache: bool = True) -> list[dict]:
        """冷启动即时填充（非运行期轮询）。

        update_cache=True（默认）：归一化结果写入 ticker_cache，供冷启动/切片构建使用。
        update_cache=False：仅返回原始 API 数据，绝不触碰 ticker_cache——用于末日期权
        循环等运行期场景，避免 book_summary 不含 greeks 的空数据覆盖 ticker 实时推送的
        真实 greeks（否则会把 delta 清零，导致 BTC 偏差检测全被剔除、前端空白）。
        """
        r = await self._send("public/get_book_summary_by_currency", {
            "currency": currency, "kind": "option"
        })
        for item in r:
            norm = self._normalize(item)
            if norm and update_cache:
                self.ticker_cache[norm["instrument_name"]] = norm
        self.last_ticker_ts = time.time()
        logger.info(f"book_summary 填充 {currency}: {len(r)} 条 (update_cache={update_cache})")
        return r

    # ============ 对外读取（纯缓存，无网络） ============
    def get_all_ticker(self, max_age: float = 300) -> dict[str, dict]:
        """返回归一化 ticker 缓存（可按时效过滤）"""
        if max_age <= 0:
            return dict(self.ticker_cache)
        now = time.time()
        return {k: v for k, v in self.ticker_cache.items() if now - v.get("ts", 0) < max_age}

    def get_cached_ticker(self, inst: str) -> Optional[dict]:
        return self.ticker_cache.get(inst)

    def get_cached_greeks(self, inst: str) -> Optional[dict]:
        """从推送缓存读实时 Greeks（无网络请求）"""
        c = self.ticker_cache.get(inst)
        if not c:
            return None
        g = c.get("greeks", {})
        return {
            "delta": g.get("delta", 0),
            "gamma": g.get("gamma", 0),
            "vega": g.get("vega", 0),
            "theta": g.get("theta", 0),
            "mark_price": c.get("mark_price") or 0,
        }

    # ============ 看门狗：监控 ticker 推送静默并自愈 ============
    async def _watchdog(self):
        while not self._shutdown:
            await asyncio.sleep(15)
            if not self.last_ticker_ts:
                continue
            silent = time.time() - self.last_ticker_ts
            if silent > 60:
                # 60s 静默：直接 close 各 ticker 连接的 ws，触发 _ticker_recv_loop
                # 异常 → _ticker_connect 重连（比 diff 重订阅有效：subscribed 集合
                # 不变时 manage_ticker_subscriptions 是空操作，无法自愈）
                logger.warning(f"看门狗: ticker 静默 {int(silent)}s，强制重连所有 ticker 连接")
                for conn in self.ticker_conns:
                    conn.running = False
                    try:
                        if conn.ws:
                            await conn.ws.close()
                    except Exception:
                        pass
                await asyncio.sleep(2)
                for conn in self.ticker_conns:
                    asyncio.create_task(self._ticker_connect(conn))
