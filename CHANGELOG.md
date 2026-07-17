# 更新日志 / Changelog

遵循语义化版本（见 `version.py`）。每次修改后递增版本号。
Semantic versioning (see `version.py`). Bump the version after every change.

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
