# 更新日志 / Changelog

遵循语义化版本（见 `version.py`）。每次修改后递增版本号。
Semantic versioning (see `version.py`). Bump the version after every change.

## [0.10.0] - 2026-07-19
- **推送通知模块**：新增 notification/ 模块，整合邮件（QQ Agent Mail）、电报、微信三种推送通道（store.py/notifier.py/channels/email.py）。
- **SABR 信号推送**：三星信号自动推送至邮箱，按 signal_id 去重（同信号每天只推一次），不同信号各自独立推送。
- **末日期权买方信号集成**：从 btc-expiry-option-buyer-signal 移植评分引擎（expiry_scorer.py），接入 Binance 永续 WS 数据（bnb_client.py），与期权天眼同进程运行。
- **Delta 符号修复**：\_finalize_signal 中 delta 计算改为持仓方向符号（买=+1，卖=-1），修复买Put+卖Call 的 delta 显示正值的问题。
- **方向/描述修正**：skew_put_cheap/skew_call_cheap/skew_put_rich/skew_call_rich 四个模式的 direction/description 全部修正。
- **权利金修复**：补回 expected_premium 赋值行，修复权利金始终显示 0 的 bug。
- **消息格式优化**：信号描述分段换行，文尾加 Z 值说明和 delta 中性对冲说明。

## [0.9.0] - 2026-07-18
- **持仓数据改为交易所实时拉取**：position_manager 重写，从 Deribit private/get_positions 读取真实持仓，不再依赖本地内存记录。重启后持仓自动恢复。
- **新增 user.changes 订阅**：交易 WebSocket 订阅 user.changes.any.any.raw 频道，持仓变更实时推送到缓存。首次连接时填充全量快照，后续增量更新，零轮询。
- **凭证持久化**：Deribit 交易凭证 + 格致邮箱密码存入本地 .env 文件（.gitignore），每次重启自动加载，无需手动填写。
- **格致对冲自动启动**：系统启动时自动登录格致并启动 BTC/ETH delta 对冲（takerEachOrderSize 按币种区分：BTC=0.05/ETH=0.1）。
- **执行信号自动启动格致**：点击"执行"后若格致未启动则自动启动，已运行则跳过。
- **前端持仓字段对齐**：更新 Position 字段名（delta/gamma/vega/theta/unrealized_pnl），修复持仓显示空白问题。

## [0.8.2] - 2026-07-18
- **新增反向偏斜检测**：`_classify_deviation_pattern` 增加 `skew_put_cheap` / `skew_call_cheap` 判定（一侧 Z<-1.2 且另一侧高出至少 1.0），`_build_signal` 增加对应买卖逻辑（买便宜腿+卖相对贵腿）。覆盖整体 IV 偏低时一侧显著便宜的偏斜场景，信号数从 2 升至 5。

## [0.8.1] - 2026-07-18
- **修复热力图 IV 色阶 bug**：ABS 模式下颜色阈值硬编码（面向 BTC 的 30-45% IV 区间），导致 ETH（正常 IV 55-70%+）持续显示红色高估。改为按币种动态计算 IV 范围做相对色阶归一化（HSL 蓝→红渐变），图例同步显示币种名称与当前 IV 区间范围。

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
