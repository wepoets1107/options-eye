# 期权天眼 更新日志

## 0.20.1 (2026-07-23) 配对纯按 Delta 对称 + 蝴蝶两翼 Delta 平衡

岛主指出做多偏斜信号再次出现 delta 失衡（Call Δ=+0.22 配 Put Δ=+0.08）。排查发现 v0.20.0 只把配对搜索范围放宽到全合约、没动评分公式，`_find_balanced_partner` 仍用 `abs_diff*0.7 + abs_z*0.3` 打分，Z 权重让"无偏离但 delta 差很远"的腿击败"delta 对称但有偏离"的腿。蝴蝶式两翼此前各自按 Z 最高独立选取，根本未做 delta 对称，且描述不展示翼 delta，无法察觉。

### 修改
- `strategy/detector.py`：`_find_balanced_partner` 评分去掉 Z 权重，改为纯按 `abs(|Δ_opp| - |Δ_primary|)` 最小选对称腿。6 种 skew 分支 + overpriced/underpriced 的 risk_reversal/strangle 配对全部受益，不再出现 delta 失衡。
- `strategy/detector.py`：butterfly（convex/concave）两翼改为 Delta 对称——以偏离更大的那翼为锚，去对侧全合约池找 |Δ| 最接近的翼；无对称翼时兜底退回各取最高 Z（不丢信号）。描述新增 `左翼Δ / 右翼Δ` 展示，便于核对两翼平衡。
- version 0.20.0 → 0.20.1。

## 0.20.0 (2026-07-22) 信号 delta 改显持仓符号 + 配对放宽到全合约

岛主指出：做空偏斜（买 Put + 卖 Call）两腿持仓 Delta 都应为负，但信号里 Put Δ=0.19、Call Δ=0.08 都显正值，未体现持仓方向；且配对只在 oi 过滤后的偏差池里找对称腿，导致主腿 delta 0.19 却配到 delta 0.08 的远 OTM Call（中间 delta 接近 0.19 的 Call 因 oi 不足被池子剔除）。

### 修改
- `strategy/detector.py`：`_fmt_delta(d, action)` 改为按买卖方向显示**持仓 Delta**（买=与期权原生符号同向、卖=反向；call + / put −），买 Put→负、卖 Call→负，两条腿都正确显负；做多偏斜（买 Call + 卖 Put）则都显正。strangle / butterfly 同步带符号。
- 新增 `_build_full_pool(kind)`：用该到期日**全合约链**（`slice_.calls`/`slice_.puts`，delta 在 [delta_min,delta_max]，不再限 oi）现算 z_score 构建配对池；`_find_balanced_partner` 改为从全合约池找对称腿，并把 `delta_min/delta_max` 透传进 `_build_signal`。之前因 oi<10 被剔除的对称腿现在能配到，delta 不再失衡。
- version 0.19.0 → 0.20.0。

## 0.19.0 (2026-07-21) 收紧 WebSocket 并发 + 修复 ticker 重连风暴

岛主指出 ticker 连接数需严格控制（≤2），且 WebSocket 订阅易触发 Deribit 限流，要求全局并发不超过 3 个。排查发现：当时实际只有 2 个 WS 连接（控制 1 + ticker 1，已 ≤3），日志里的"批次 2/8"是订阅分批轮转而非连接数；真正导致 429 刷屏的是**重连风暴**——ticker 连接被拒后仅 sleep(3) 即无限重连，维护期可能累积多个重连协程并发握手，反而把限流打得更死。

### 修改
- `data/deribit_ws.py`：新增 `MAX_TICKER_CONNS=2`（ticker 连接硬上限）、`MAX_WS_TOTAL=3`（全局 WS 并发硬上限，控制 1 + ticker ≤2）、`TICKER_CONNS_ACTIVE=1`（实际启用数，最稳且握手频率最低）。`manage_ticker_subscriptions` 创建循环加全局并发保护（控制 1 + 当前 ticker 数 ≥ 上限则停止新建）。
- 新增 `_calc_backoff(attempt, is_429)`：429 限流退避 60→120→240→300s 封顶，普通错误 5→10→...→120s 封顶。
- `_ticker_connect` 重写：入口加 `conn._reconnecting` 标志，**防止同一 conn 多个重连协程并发累积**（这是握手风暴的根因）；429 判定扩展为 "429"/"Too Many"/status==429；原 `sleep(3)` 无脑重连改为 `_calc_backoff` 退避。
- `_ticker_recv_loop` 异常重连改为交给 `_ticker_connect` 统一退避（入口拦截并发），不再自睡眠后裸重连。

### 说明
- 实际 ticker 连接维持 1 个（满足"≤2"），分批轮转覆盖全量合约的架构不变；如需更快覆盖可上调 `TICKER_CONNS_ACTIVE` 至 2，但握手频率翻倍、更易 429，非必要不动。
- 若 Deribit 临时维护，重连退避会让节奏自动放缓（60s 起），不会因频繁握手加剧限流。

## 0.18.0 (2026-07-21) SABR 合并推送冷却 8 小时 → 4 小时

岛主认为 8 小时冷却偏长，medium/ETH 信号刚冒头要等很久才推。现将 `push_sabr_signals` 的合并推送冷却窗口由 8 小时缩短为 4 小时。排重（按 signal_id 每日去重 + 跨周期模糊去重）、分片发送、强度排序等逻辑全部不变。

### 修改
- `notification/notifier.py`：`COOLDOWN_SEC` 由 `8 * 3600` 改为 `4 * 3600`；docstring 同步更新为「每 4 小时最多推送 1 次」。

### 说明
- 冷却只管推送频率（节流/聚合），不负责去重；去重仍由稳定 signal_id 与腿级模糊匹配保证，缩短冷却不影响去重强度。
- 每轮扫描新冒出的、id 全新的信号会在下一个冷却窗口（≤4 小时）被合并推送，推送更及时，但仍不至于刷屏。

## 0.17.0 (2026-07-21) SABR 信号推送放开 medium 档

岛主希望电报群能看到更多 ETH 信号（ETH IV 曲面异常偏弱，多数只够到 medium 档，原逻辑只推 high 三星导致推送几乎全是 BTC）。现将推送门槛从仅 high 扩大到 high + medium（low 仍不推）。

### 修改
- `notification/notifier.py`：`push_sabr_signals` 的候选过滤由 `confidence != "high"` 改为 `confidence not in ("high", "medium")`。
- 候选信号按强度（high 优先，其次按 |Z| 降序）排序后**分片发送**，每片最多 15 条，避免单条电报消息超过 4096 字符上限而推送失败。多片时在标题注明「第 x/N 批，共 M 条」。
- 保留原有 8 小时冷却、按 signal_id 每日去重、跨周期模糊去重（同策略+共享腿）。

### 说明
- medium 信号数量远大于 high（当前偏差池 medium 占多数），放开后每次合并推送窗口信号会显著变多，电报群可能连发多条；若觉得刷屏可再调阈值或恢复仅 high。
- 8 小时冷却与每日去重不变，单条信号当天最多推 1 次。

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
