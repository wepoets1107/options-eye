# 更新日志 / Changelog

遵循语义化版本（见 `version.py`）。每次修改后递增版本号。
Semantic versioning (see `version.py`). Bump the version after every change.

## [0.8.0] - 2026-07-18
- **P0-1 修复持仓 PnL 符号反转**：`_finalize_signal` 与 `_update_position_greeks` 的权利金口径统一为净现金流（sell=+收 / buy=-付），`pnl=entry-current` 现在卖出策略盈利时正确显示正数。
- **P0-2 修复信号 id 不稳定**：信号 id 改为基于内容哈希（currency+expiration+strategy_type+legs），主循环多轮重建信号 id 不变，确认/忽略状态不再丢失。
- **P1-1 修复热力图 put delta 匹配**：`/api/slices` 的 puts delta 去掉 `abs`，保持原始负值，负 delta bucket 正确匹配 put 合约。
- **P1-2 参数滑块生效**：新增 `/api/params` 接口（GET/POST），前端滑块 change 即时下发，主循环读取 `runtime_params` 覆盖 config 默认；页面加载时从后端回填滑块值。
- **P1-3 平仓对冲量准确回写**：`api_execute` 执行后从 `results` 取实际成交对冲量回写 `pos.hedge_amount`，hedge_skipped 时置 0，平仓不再误平不存在的对冲单。
- **P1-4 格致 stop 传币种**：`glStop` 带当前下拉框 currency，停止 ETH 对冲不再误停 BTC。
- **P1-5 看门狗自愈有效化**：ticker 静默>60s 直接 close 连接触发重连（原 diff 重订阅在 subscribed 不变时是空操作）。
- **P1-6 执行部分成交告警**：`executeSignal` 解析每腿状态，失败/对冲跳过时高亮提示"存在单腿敞口"。
- **P2 批量优化**：
  - 删除死代码 `get_delta_filtered_contracts` / `compute_bartlett_delta` / overpriced 分支错误 hedge 公式。
  - SABR 校准只用 OTM 合约（call: K>F / put: K<F），剔除 ITM 噪声；收敛 RMSE 阈值 0.5→0.05。
  - `deribit_trader` 弃用 `get_event_loop` 改 `get_running_loop`；`greeks_live_hedge` token 过期自动重登重试。
  - `_classify_deviation_pattern` wing/near 分档阈值动态化（跟随 delta_min/delta_max），overpriced 增加绝对偏差辅助判定（防 sigma 压缩漏检）。
  - `switchView/switchCurr` 用 id 替代 querySelectorAll 索引；`signal_status_overrides` 自动清理（>50 条时只保留当前活跃信号）。

## [0.7.0] - 2026-07-17
- **前端体验优化（四块）**：
  - IV 曲面热力图放大：单元格 `min-width 38→66`、`height 24→46`、字号 `10→13`；容器宽度上限 `1400→1860px`；新增颜色图例（绝对值 / SABR 偏差双模式）+ 异常描边图例；sticky 表头、hover 放大。
  - 策略持仓新增 **未实现盈亏 PnL**：后端 `Position` 增加 `entry_premium/current_premium/pnl/pnl_pct`，在 `update_greeks` 中用实时 `mark_price` 计算（期权腿，不含永续对冲腿）；逐笔持仓与汇总行均红绿着色展示，附入场权利金/当前市值。
  - 格致 Trial Forge 对冲模块新增**参数与仓位面板**：前端暴露"目标 Delta / 偏离带"输入，启动接口透传；状态面板结构化展示运行状态、币种、目标 Delta、偏离带、订单类型、对冲标的、对冲比例、启动时间。
  - 整体打磨：顶部渐变导航、侧栏 sticky、卡片间距/圆角/配色对比、状态栏留刷新提示。

## [0.6.0] - 2026-07-17
- **修复策略信号产出率过低（体感"空白"）**：
  - 放宽 `spread_filter_pass`：偏差达到买卖 IV 点差一半即视为有效（原要求超过点差，导致 71% 偏差被硬过滤）。
  - 放宽 `_classify_deviation_pattern` 模式阈值：曲率 wing z `1.0→0.8`、整体/偏斜 z `1.5→1.2`、偏斜差值 `1.5→1.0`。
  - 修复逻辑不一致：`generate_signals` 现把**全部偏差**传给 `_build_signal` 配对腿（spread 仅作评级，不再硬过滤导致"有模式却造不出信号"）；medium 置信的 z_avg 门槛 `2.0→1.2`。
- **修复展示层序列化 bug**：`/api/deviations` 补齐 `currency`/`expiration` 字段，字段名与 `IVDeviation` 一致（`spread_ok/oi_ok` → `spread_filter_pass/oi_filter_pass`）。

## [0.5.0] - 2026-07-17
- **修复 ticker 推送间隔**：Deribit 公共连接实测仅 `100ms`（及 `raw` 需认证）真正推送；`1s/5s/1m` 均不推送。默认间隔由 `5s` 改为 `100ms`，解决"订阅成功但零推送、看门狗误触发"的问题。
- **纯推送架构落地**：行情数据全部由 `ticker.{instrument}.{interval}` 订阅推送，运行期零轮询（此前主循环每轮 `get_book_summary_by_currency` 轮询已移除）。
- 新增 `version.py` 版本号，并在 `/api/status` 与 README 暴露。
- 新增 `config.example.yaml` 模板 + `load_config` 回退；`config.yaml` 不入库（`.gitignore`）。
- 新增双语 README、MIT LICENSE、`.gitignore`。

## [0.4.0] 前期开发（测试网实测）
- 审计修复 P0/P1/P2：看门狗自愈、真实 pa Greeks 全链路落地、对冲腿合约单位处理、确认/忽略状态持久化等。
- 测试网实测：交易执行（期权两腿 + 对冲腿）、持仓 Greeks 即时刷新、反向平仓、格致对冲 login/start/status/stop 全通过。
