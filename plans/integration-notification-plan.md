# 期权天眼 + 末日期权买方信号 整合计划

## 目标

以期权天眼（options-eye）为主项目，新增推送模块。策略信号通过电报/微信/邮件推送：

- 信号源 1：期权天眼的 **SABR IV曲面异常信号**（每天只推一次）
- 信号源 2：**末日期权买方信号**（评分触发即推，有冷却）
- 推送渠道：电报群 / 微信 / QQ Agent Mail

## 现状分析

### 期权天眼（主体）
- ✅ IV曲面异常扫描 → SABR偏差 → 策略信号生成（6种模式）
- ✅ Web工作台（信号卡片/持仓/格致状态）
- ✅ 策略执行 + 格致对冲 + 持仓管理
- ❌ 无推送能力（信号只在浏览器展示）

### 末日期权买方信号
- ✅ 评分引擎：趋势/动量/资金流/期权盘口/IV分位 → BUY_CALL/PUT/STRADDLE
- ✅ 微信推送（openclaw 命令）
- ✅ 冷却机制（cooldown + 每日配额）
- ❌ 无电报推送
- ❌ 数据源独立，不和期权天眼共用

### 现有邮件工具
- ✅ QQ Agent Mail（`agently-cli.CMD`） — 不走SMTP

## 整合方案

在期权天眼内新增 `notification/` 模块 + 把末日期权买方信号引擎集成进去。

```
options-eye/
├── notification/                   # 新增 —— 推送模块
│   ├── __init__.py
│   ├── notifier.py                # 推送管理器（统一入口）
│   ├── channels/
│   │   ├── wechat.py              # 微信推送（openclaw 命令）
│   │   ├── telegram.py            # 电报推送（HTTP bot API）
│   │   └── email.py               # 邮件推送（QQ Agent Mail CLI）
│   ├── expiry_scorer.py           # 末日期权买方评分引擎（移植自 btc-expiry-option-buyer-signal）
│   └── store.py                   # 推送状态持久化（已推信号/冷却/配额）
├── config.yaml                    # 增加推送配置段
├── main.py                        # 修改：初始化推送模块
├── web/app.py                     # 轻微修改
```

## 实施步骤

### Step 1：推送状态持久化（store.py）

推送记录写入 `/var/lib/options-eye/notifications.jsonl` 和 `pushed_signals.json`，用于：
- 已推送信号去重（SABR 信号每天只推一次）
- 冷却检查（末日期权信号有冷却时间）
- 每日推送次数计数

### Step 2：SABR 信号推送（每天一次）

在 `main.py` 主循环中，每轮信号生成后：

```
1. detect_deviations() → generate_signals() → signals[]
2. 按 currency 合并当天所有 SABR 信号：
   - 取置信度最高的 1-2 条
   - 合并为一条推送消息
3. 检查 pushed_signals.json：
   - 当天是否已推过 SABR 信号 → 已推则跳过
   - 未推则推送并记录
```

消息格式示例（推送到电报群/微信/邮件）：

```
[期权天眼] BTC 策略信号

做空偏斜 Risk Reversal — 置信度 high
买 BTC-28AUG26-74000-C + 卖 BTC-28AUG26-54000-P
预估权利金: +0.0012 BTC | 净 Δ: +0.271
详情: https://binghuodao.club/options-eye

━━━

做多曲率 Butterfly — 置信度 medium
买 BTC-25SEP26-70000-C + 卖 2x BTC-25SEP26-80000-C + 买 BTC-25SEP26-90000-C
预估权利金: -0.0005 BTC | 净 Δ: +0.008

信号由 SABR IV 曲面异常扫描生成，不构成交易建议。
```

### Step 3：末日期权买方信号集成

从 `btc-expiry-option-buyer-signal/monitor.py` 移植核心评分逻辑到 `notification/expiry_scorer.py`：

- Binance 永续数据（通过 Deribit index price + 实时价格近似，或单独拉 Binance WS）
- RollingMarket / VolRegime / OITracker / WhaleTracker 等数据容器
- SignalEngine.evaluate() 的评分逻辑

运行方式：
1. 作为 options-eye 主循环的附加任务，每 30 秒跑一次
2. 评分结果通过 notifier 推送
3. 冷却：12 小时冷却 + 每日最多 2 次（沿用末日期权项目的参数）

消息格式：

```
[末日期权信号] BTC BUY_CALL

评分: Call 72 | Put 35 | 双买 41
合约: BTC-28AUG26-ATM Call
剩余时间: 5.2 小时
ATM IV: 42.3% (32% 分位)

趋势: 价格 > VWAP, EMA20 > EMA60
动量: 5m +0.32%, 放量 2.1x
资金流: 主动买 58%
期权 PCR: 0.72 (偏低偏 Call)

不构成交易建议。
```

### Step 4：电报推送（telegram.py）

复用 `skills/binghuodao-options-notes/scripts/send_telegram.py` 的 HTTP API 推送逻辑：
- 通过 `https://api.telegram.org/bot{token}/sendMessage`
- 纯文本，不加 Markdown
- 配置：bot_token + chat_id 从 `.env` 读取或 config.yaml

### Step 5：微信推送（wechat.py）

复用末日期权项目的 Notifier：
- 调用 `openclaw message send --channel openclaw-weixin --target xxx --message text`
- 配置：channel/target/account 从 config.yaml 读取
- 异步执行，不阻塞主循环
- 无 openclaw 环境时跳过

### Step 6：邮件推送（email.py）

使用 QQ Agent Mail CLI 发送，不走 SMTP：
- 调用 `agently-cli.CMD mail send --to binghuodao@agent.qq.com --subject "xxx" --body "xxx"`
- 配置：from/to 地址从 `.env` 读取
- 正文纯文本
- 每日限额保护

## 配置（添加到 config.yaml）

```yaml
# 推送配置
notification:
  enabled: true

  # SABR 信号 — 每天只推一次
  sabr:
    enabled: true
    push_interval: "daily"         # daily / each

  # 末日期权买方信号
  expiry_buyer:
    enabled: true
    eval_interval_sec: 30          # 评分频率
    cooldown_sec: 43200            # 同类信号冷却（12小时）
    max_per_day: 2                 # 每日最大推送次数

  wechat:
    enabled: true
    channel: "openclaw-weixin"
    target: "o9cq804TZ1g7CbmoZyjvXOCi-qiA@im.wechat"
    account: "8dbb069b7560-im-bot"

  telegram:
    enabled: true
    bot_token: ""                  # 从 .env 读取 TELEGRAM_BOT_TOKEN
    chat_id: ""                    # 从 .env 读取 TELEGRAM_CHAT_ID

  email:
    enabled: true
    from_addr: "binghuodao@agent.qq.com"
    to_addr: "9006549@qq.com"
```

## 依赖

- 电报推送：aiohttp（options-eye venv 已有）
- 微信推送：openclaw CLI（需安装）
- 邮件推送：agently-cli.CMD（已有）
- 末日期权评分引擎：需要 Binance 永续数据（新增 websocket 依赖，options-eye venv 已有 websockets）

## 注意事项

1. **SABR 信号去重**：以"当天日期 + currency"为 key，每天只推一次。重启后读取 pushed_signals.json 避免重复。
2. **末日期权信号冷却**：冷却状态持久化到 JSON，重启后不会丢失。
3. **异步推送**：推送任务放到 asyncio.create_task，不阻塞主循环。
4. **通道独立性**：每个通道独立 try/except，一个通道失败不影响其他通道。
5. **隐私**：bot token / chat_id 放 .env，不提交到 GitHub。
