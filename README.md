# 期权天眼 · Options Eye

> Deribit 期权 IV 曲面异常扫描器 · 策略建议 · 一键执行 · 自动推送
> Deribit options IV-surface anomaly scanner · strategy suggestions · one-click execution · push notifications

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/Python-3.13+-blue.svg)](https://www.python.org)
[![Version](https://img.shields.io/badge/version-0.10.0-green.svg)](version.py)

---

## 中文说明

### 这是什么

期权天眼是一个基于 Deribit 公共 WebSocket 实时数据的 BTC / ETH 期权分析工具。

它用 SABR 模型校准"正常"的隐含波动率（IV）曲面，扫描市场 IV 相对基准的异常偏离，给出可执行的期权策略建议，并支持在测试网一键下单、对冲与平仓。策略信号可通过邮件/微信自动推送。

### 核心特性

- **纯推送、零轮询**：行情数据全部通过 Deribit `ticker.{instrument}.{interval}` 订阅频道实时推送。**单连接分批轮转采集**，任何时候只有 1 个 WebSocket 连接、≤200 个活跃订阅，不对 Deribit 构成并发压力。
- **SABR 基准 IV**：本地最小二乘校准 α / β / ρ / ν，算出每个到期日的期望 IV 曲面。
- **Z-score 异常检测**：市场 IV 偏离 SABR 期望 IV 超过阈值即报警，并归类为 α / ρ / ν 异常。
- **真实 Greeks**：从 `public/ticker` 推送注入真实 delta / gamma / vega / theta，作为全链路基准。
- **策略体系**：涵盖 Straddle / Strangle（波动率）、Risk Reversal（偏斜）、Butterfly（曲率）等 6 类场景。
- **一键交易**：Web 工作台展示异常 → 策略建议（含预估权利金 / 净 delta / 对冲腿）→ 确认 → 测试网下单。
- **对冲与平仓**：期权两腿 + 永续对冲腿（由格致 Trial Forge 自动管理 delta 中性）。
- **自动推送**：三星策略信号通过 QQ Agent Mail / 微信自动推送，按 signal_id 去重（同信号每天只推一次）。

### 快速开始

```bash
# 1. 准备 Python 3.13+ 虚拟环境
python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate

# 2. 安装依赖
pip install -r requirements.txt

# 3. 配置
cp config.example.yaml config.yaml
# 按需编辑 config.yaml（无需填入任何密钥；交易密钥通过 Web 界面输入）

# 4. 启动
python main.py
# 打开 http://127.0.0.1:5051
```

### 配置说明

所有参数在 `config.yaml`（参考 `config.example.yaml`）。关键点：

- `deribit.ticker_interval`：ticker 推送间隔。**仅 `100ms` 与 `raw`（需认证）在公共连接上实际推送**；`1s` / `5s` / `1m` 在公共 API 上实测不推送，请勿使用。
- `filters`：策略范围（到期日、Delta 区间、OI、成交量、Z-score 阈值）。
- `sabr`：SABR 模型参数边界与校准节奏。
- `notification`：推送通知配置（邮件/微信开��）。
- `web`：Web 工作台监听地址与端口。

### 目录结构

```
data/           Deribit WebSocket 客户端（推送订阅、单连接分批轮转、看门狗）
sabr/           SABR 校准引擎
strategy/       IV 异常检测与信号生成
execution/      Deribit 交易客户端、持仓管理、格致对冲
web/            FastAPI 工作台（前端 HTML + 后端 API）
notification/   推送通知模块（邮件/微信/电报通道、评分引擎、状态持久化）
plans/          设计文档与计划
```

### 架构说明

```
Deribit WS (ticker 100ms 推送, 1连接分8批轮转)
    ↓ ticker_cache
build_chain_snapshot()  →  ExpirySlice / OptionContract (含实时 Greeks)
    ↓
SABR calibrate_all()   →  SabrParams (α/ρ/ν)
    ↓
detect_deviations()    →  IVDeviation[] (偏差 pt + Z-score)
    ↓
generate_signals()     →  Signal (策略建议，6种模式)
    ↓
[Web 工作台] ← → [推送通知模块]
    ↓                    ↓
  用户确认执行         邮件/微信自动推送
    ↓
Deribit 测试网下单 + 格致 delta 对冲
```

### 推送通知

三星（high）置信度的策略信号会在首次出现时自动推送：

- **邮件**：通过 QQ Agent Mail 发送到指定邮箱（默认 `binghuodao@agent.qq.com` → `9006549@qq.com`）
- **微信**：通过 wx-send 工具发送（可选，需服务端环境）
- **电报**：待接入（占位）

推送规则：
- 同一信号（同币种+同行权价+同到期日）24 小时内只推一次
- 不同信号各自独立推送
- 每天不限总次数，按信号内容去重

### ⚠️ 风险提示

本项目仅用于研究与学习。所有交易在 **Deribit 测试网** 执行，不构成任何投资建议。期权交易风险极高，请勿直接用于实盘。

---

## English

### What is this

Options Eye is a real-time BTC / ETH options analytics tool built on Deribit's public WebSocket data.

It calibrates a "normal" implied-volatility (IV) surface with the SABR model, scans the market IV for anomalous deviations from that baseline, suggests actionable option strategies, and supports one-click testnet ordering, hedging, and closing. Strategy signals can be automatically pushed via email / WeChat.

### Key features

- **Push-only, zero polling**: All market data delivered via Deribit `ticker.{instrument}.{interval}` subscriptions. **Single-connection batch rotation**: only 1 WebSocket connection with ≤200 active subscriptions at any time, no concurrent connection pressure on Deribit.
- **SABR baseline IV**: Local least-squares calibration of α / β / ρ / ν to build the expected IV surface per expiry.
- **Z-score anomaly detection**: Flags market IV deviations beyond a threshold from the SABR baseline.
- **Real Greeks**: Delta / gamma / vega / theta injected from `public/ticker` pushes.
- **Strategy system**: 6 scenarios — Straddle / Strangle (volatility), Risk Reversal (skew), Butterfly (curvature), plus overpriced/underpriced detection.
- **One-click trading**: Web workbench → anomaly display → strategy suggestions (premium / net delta / hedge) → confirm → testnet order.
- **Auto hedging**: Perpetual delta hedge managed by Gezhi (GreeksLive) Trial Forge.
- **Push notifications**: High-confidence signals auto-pushed via email / WeChat, deduplicated by signal_id.

### Quick start

```bash
python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
cp config.example.yaml config.yaml
python main.py
# open http://127.0.0.1:5051
```

### Project structure

```
data/           Deribit WS client (push subscriptions, single-conn batch rotation, watchdog)
sabr/           SABR calibration engine
strategy/       IV anomaly detection & signal generation
execution/      Deribit trading client, position management, Gezhi hedge
web/            FastAPI workbench (frontend + backend API)
notification/   Push notification module (email/wechat/telegram channels, expiry scorer, state persistence)
plans/          Design docs and plans
```

### ⚠️ Disclaimer

For research and education only. All trades execute on the **Deribit testnet** and do not constitute investment advice. Options trading carries extreme risk — do not use in production without proper verification.

---

## ☕ 打赏 / Donate

如果这个项目对你有帮助，欢迎打赏支持冰火岛社区持续产出。

If this project helped you, tips are welcome to support the Binghuodao community.

**EVM 钱包 / EVM wallet:**
`0x29f091DAA3dfee8100645ee24239bCC3ae174B42`

（支持 ETH / ARB / BASE / 等 EVM 链 · Supported on ETH / ARB / BASE / any EVM chain）

---

## License

[MIT](LICENSE) © 2026 wepoets1107
