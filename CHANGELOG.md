# 期权天眼 更新日志

## 0.16.0 (2026-07-21) 修复 BTC SABR 偏差前端空白（数据源隔离）

末日期权循环每轮调用 `get_book_summary_by_currency("BTC")` 会顺手把归一化结果写回 `ticker_cache`，而 book_summary 不含 greeks，于是把 ticker 实时推送的 BTC 真实 greeks（含 delta）反复清空成 0。detector 的 `0.05 ≤ |delta| ≤ 0.25` 硬门槛把 BTC 合约全踢掉，导致 BTC 0 偏差、前端「BTC SABR 偏差」整片空白（ETH 不受影响，因为末日期权只看 BTC、从不灌 ETH 的 book_summary）。

### 修复（方案 B：彻底隔离两条数据流）
- `data/deribit_ws.py`：`get_book_summary_by_currency` 新增 `update_cache: bool = True` 参数。`update_cache=False` 时仅返回原始 API 数据、**绝不写回 `ticker_cache`**，避免空 greeks 覆盖 ticker 推送的真实 greeks。
- `main.py`：末日期权循环对该调用传 `update_cache=False`，book_summary 结果用独立变量 `opts` 承载，与 `ticker_cache` 完全解耦；REST 兜底段只读 `ticker_cache` 取真实 greeks（不改写）。

### 验证
- BTC 切片 delta 有值率：2.7%（13/476）→ 98.9%（471/476）
- BTC 偏差数：0 → 101（ETH 53，共 154）；前端 BTC SABR 偏差恢复显示

## 0.15.0 (2026-07-20) 交叉检查 bug 修复（连接健壮性 + 末日期权评估容错）

对上一轮修改做代码交叉检查，修复 8 个 bug（P0~P2），提升连接自愈与评估容错。

### 修复
- `data/deribit_ws.py`：
  - **看门狗协程泄漏（P1）**：`connect()` 中 `asyncio.create_task(self._watchdog())` 未保存引用、`while True` 无退出、`disconnect` 不取消。现保存 `_watchdog_task`，循环加 `not self._shutdown` 退出判断，`disconnect` 中一并 cancel。
  - **重连后指数订阅永久丢失（P1）**：`_supervisor` 中 `subscribe_index` 失败且 recv 仍存活时会被卡在 sleep 分支、指数订阅再也不会恢复。现由 `_index_subscribed` 标志独立重试订阅（连接健康但订阅未完成时持续重试，不依赖 recv 断开）。
  - **429 限流判定漏匹配（P2）**：`_supervisor` 原仅用 `"429" in str(e)` 判限流，可能漏掉 websockets `InvalidStatusCode`。现同时检查 `e.status == 429`。
  - **ticker 退避重置（P2）**：`_ticker_recv_loop` 重连不传 `retry_count`，导致 `over_limit` 退避从 0 重算。现 `_TickerConn` 记录 `retry_count`，recv 循环重连时续传。
- `main.py`：
  - **REST 兜底阻塞事件循环（P0）**：末日期权指数价与 `book_summary` 的 REST 兜底原用同步 `urllib.request.urlopen`，控制连接断开时每轮阻塞数秒~十几秒、卡住整条事件循环。现统一改用 `asyncio.to_thread` 包装的非阻塞 `_rest_get_json`，并同步用于 Binance 历史回填。
  - **循环内重复 import（P2）**：去掉循环内 `import json, urllib.request`，提到文件顶部。
  - **数据源不可用快照冻结（P2）**：`expiry_eval_loop` 中 `idx`/`opts` 都为空时 `last_scores` 不更新，前端长期显示陈旧快照。现新增 `data_unavailable` 状态，明确暴露「WS+REST 均失败」。
- `notification/expiry_scorer.py`：
  - **主计算段异常冻结快照（P1）**：`evaluate()` 主计算段（方向/资金流/选合约/双买评分 + 落盘）原无 try/except，脏数据会导致异常上抛、快照冻结且无提示。现包裹该段，异常时写 `last_scores` 为 `error` 状态并 `return None`，前端可立即看出评估失败。

## 0.14.0 (2026-07-20) 修复前端页面数据空白

`index.html` 中 `fmtBJT()` 时间格式化函数被误放在 `renderExpiry` 内部（局部作用域），导致全局 `fetchData` 调用时报 `fmtBJT is not defined`，整页数据渲染中断、页面全空白。已将 `fmtBJT` 提升到全局作用域修复。

## 0.13.0 (2026-07-20) 展示时间统一 UTC+8

所有面向用户的时间展示统一为北京时间（UTC+8），不再依赖浏览器/机器时区。

### 修改
- `notification/expiry_scorer.py`：`fmt_ts()` 的时区常量由 UTC 改为 UTC+8（`updated_at` 字段现在显示北京时间）。
- `web/templates/index.html`：新增 `fmtBJT()`，将头部更新时间、持仓成交时间、服务启动时间由 `toLocaleString`（随浏览器时区）改为显式 UTC+8 格式化。
- 内部 UTC 计算（Deribit 到期日比较、SABR 剩余时间等）保持不变。

## 0.12.0 (2026-07-20) 控制连接重连根治 + 末日期权 REST 兜底

修复末日期权评分因 Deribit 控制连接断开而永久冻结的故障。

### 修复
- `data/deribit_ws.py`：控制连接重连从「recv 协程内递归重启」改为独立 `_supervisor` 协程管理。
  - `_connect_main` 重连前先 cancel 旧 recv 协程，杜绝并发 recv（websockets 报
    "cannot call recv while another coroutine is already running" 导致连上即断的死循环）。
  - `_control_recv_loop` 改为单次生命周期，断开后由 `_supervisor` 负责重连，不再自重启。
  - `_supervisor` 无限重试（去掉原 10 次硬上限），指数退避；识别 HTTP 429 限流时退避 60→120→240→300s，
    普通失败 5→10→20→40→80→120s 封顶。
  - `disconnect` 置 `_shutdown` 并取消 supervisor/recv 协程，避免关闭后继续重连。
- `main.py`：`expiry_eval_loop` 拉合约时，WS 控制连接异常或返回空，自动用 Deribit REST
  `get_book_summary_by_currency?currency=BTC` 兜底，末日期权评估不再依赖控制连接。

## 0.11.0 (2026-07-20) 末日期权买方信号前端模块

前端新增「末日期权买方信号」模块（位于策略信号下方），实时展示评分，不再只等触发才推电报。

### 新增
- 评分引擎 `notification/expiry_scorer.py`：每轮评估都写 `self.last_scores`（含 Call/Put/双买评分、价格/VWAP、动量、PCR、ATM IV、剩余分钟、末日期权数、状态、触发原因），未触发也保留，last_signal 默认 `NO_TRADE`。
- `main.py`：`expiry_eval_loop` 每轮把 `scorer.last_scores` 写入共享 `web_state["expiry_state"]`；启动时初始化该字段。
- `web/app.py`：新增 `GET /api/expiry` 返回末日期权评分快照。
- `web/templates/index.html`：新增「末日期权买方信号」卡片，轮询 `/api/expiry`（随主循环 3 秒刷新），展示信号、三评分进度条、动量/资金流、到期与样本、触发原因，预热/等待/无到期状态给出提示。
- 模式对齐私有仓库 `btc-expiry-option-buyer-signal` 的「每轮落盘 + 前端轮询」方案。

## 0.10.1 (2026-07-20) 数据入口 Bug 修复

交叉检查发现的 Bug 修复（详见代码注释）：

### 严重
- 指数价订阅频道修正：`deribit_price_index.raw` 在实盘无效，改为
  `deribit_price_index.btc_usd` / `deribit_price_index.eth_usd`；
  `main.py` 主循环读取键从 `btc_usdc`/`eth_usdc` 改为 `btc_usd`/`eth_usd`。
  修复后主循环 SABR 链拿到真实指数价，moneyness / ATM IV 计算正确。
- ticker 轮转循环防重复：新增 `_rotation_started` 标志，`manage_ticker_subscriptions`
  只在首次创建 `_ticker_rotation_loop`，合约刷新时不再重复 `create_task`，
  避免多循环共享批次索引导致订阅 churn / 触发 over_limit。

### 高
- ticker 连接重连后清空 `conn.subscribed`（`_ticker_connect`），强制下一轮轮转
  重新订阅，修复「重连后空转→看门狗死循环重连」问题。

### 中
- 持仓币种字段修正：`position_manager.py` 从 `ex_data["currency"]` 读取币种，
  原代码误用 `direction`（buy/sell/zero）导致 Web 持仓页币种显示错乱。
- 移除 Binance `@bookTicker` 流及其 `add_book_tick`：该流用中间价涨跌伪造成交并
  覆盖真实成交量，污染主动买卖比 / VWAP / 放量倍数。删除后仅保留
  aggTrade / kline_1m / markPrice 三个真实数据源。

### 低
- `store.py` 保留期清理改用本地日期（`time.localtime`），与推送 key 的本地日期一致，
  修正非 UTC 时区的边界差一天问题。
- `notifier.py` 为 `push_sabr_signals` 增加 `asyncio.Lock` 串行锁，覆盖冷却判断到
  发送全过程，防止并发 `create_task` 下同一批次重复推送。
- `deribit_trader.py` 删除重复的 `get_cached_positions` 定义。
- `main.py` 末日期权评分用 ticker 缓存里的真实 greeks 补齐 book_summary，
  让 `_choose_option` 的 delta 选合约逻辑真正生效（此前 book_summary 无 greeks 只能
  走 moneyness 兜底）。
