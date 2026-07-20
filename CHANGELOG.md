# 期权天眼 更新日志

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
