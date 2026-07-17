# 更新日志 / Changelog

遵循语义化版本（见 `version.py`）。每次修改后递增版本号。
Semantic versioning (see `version.py`). Bump the version after every change.

## [0.5.0] - 2026-07-17
- **修复 ticker 推送间隔**：Deribit 公共连接实测仅 `100ms`（及 `raw` 需认证）真正推送；`1s/5s/1m` 均不推送。默认间隔由 `5s` 改为 `100ms`，解决"订阅成功但零推送、看门狗误触发"的问题。
- **纯推送架构落地**：行情数据全部由 `ticker.{instrument}.{interval}` 订阅推送，运行期零轮询（此前主循环每轮 `get_book_summary_by_currency` 轮询已移除）。
- 新增 `version.py` 版本号，并在 `/api/status` 与 README 暴露。
- 新增 `config.example.yaml` 模板 + `load_config` 回退；`config.yaml` 不入库（`.gitignore`）。
- 新增双语 README、MIT LICENSE、`.gitignore`。

## [0.4.0] 前期开发（测试网实测）
- 审计修复 P0/P1/P2：看门狗自愈、真实 pa Greeks 全链路落地、对冲腿合约单位处理、确认/忽略状态持久化等。
- 测试网实测：交易执行（期权两腿 + 对冲腿）、持仓 Greeks 即时刷新、反向平仓、格致对冲 login/start/status/stop 全通过。
