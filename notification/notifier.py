"""
推送管理器 — 整合多个推送通道，处理信号格式化/去重/日志

用法:
    notifier = NotificationManager(config)
    await notifier.push_sabr_signals(signals_list)
"""
import logging
import time
from typing import Optional

from notification.store import push_count_today, mark_pushed, append_log
from notification.channels.email import send_email

logger = logging.getLogger(__name__)


class NotificationManager:
    """推送管理器"""

    def __init__(self, config: dict):
        self.cfg = config.get("notification", {})
        self.enabled = self.cfg.get("enabled", False)

        # 通道配置
        self.email_cfg = self.cfg.get("email", {})

    async def push_sabr_signals(self, signals: list[dict]) -> int:
        """推送 SABR 策略信号

        规则：
        - 仅推送 confidence=high（三星）的信号
        - 同一信号（signal_id）24小时内只推一次
        - 不同信号（不同行权价/到期日）各自独立推送
        """
        if not self.enabled or not signals:
            return 0

        pushed = 0
        for s in signals:
            if s.get("confidence") != "high":
                continue

            key = f"sabr_{s.get('id', '')}"
            cnt = push_count_today(key)
            if cnt >= 1:
                logger.info(f"信号 {key[:20]}... 当天已推送，跳过")
                continue

            body = self._format_sabr_message(s, [s])
            ok = await self._dispatch(body, f"[期权天眼] {s.get('currency', '')} 策略信号")
            if ok:
                mark_pushed(key)
                append_log({
                    "ts": int(time.time()),
                    "type": "sabr",
                    "signal_id": s.get("id", ""),
                    "currency": s.get("currency", ""),
                    "strategy": s.get("strategy_type", ""),
                    "direction": s.get("direction", ""),
                    "description": s.get("description", "")[:80],
                })
                pushed += 1

        logger.info(f"SABR 推送完成: {pushed} 条新信号")
        return pushed

    async def push_expiry_signal(self, sig: dict) -> bool:
        """推送末日期权买方信号（24小时内最多2次）"""
        if not self.enabled or not sig:
            return False

        key = "expiry"
        cnt = push_count_today(key)
        if cnt >= 2:
            logger.info(f"末日期权信号当天已推送 {cnt} 次，已达上限")
            return False

        body = self._format_expiry_message(sig)
        ok = await self._dispatch(body, "[期权天眼] 末日期权信号")
        if ok:
            mark_pushed(key)
            append_log({
                "ts": int(time.time()),
                "type": "expiry_buyer",
                "signal": sig.get("signal", ""),
                "confidence": sig.get("confidence", 0),
                "contract": sig.get("contract", ""),
                "call_score": sig.get("call_score"),
                "put_score": sig.get("put_score"),
                "straddle_score": sig.get("straddle_score"),
            })
        return ok

    def _format_expiry_message(self, sig: dict) -> str:
        signal_cn = {"BUY_CALL": "买入Call", "BUY_PUT": "买入Put", "BUY_STRADDLE": "双买"}.get(
            sig.get("signal", ""), sig.get("signal", ""))
        met = sig.get("metrics", {})
        flow = sig.get("flow", {})
        lines = [
            f"【末日期权信号】BTC {signal_cn}",
            f"信号强度: {sig.get('confidence', 0)}",
            f"合约: {sig.get('contract', '')}",
            f"剩余到期: {sig.get('minutes_left', 0):.0f}分钟",
            f"现价: {met.get('price', 0):,.1f} | VWAP: {met.get('vwap', 0):,.1f}",
            f"评分: Call {sig.get('call_score', 0)} / Put {sig.get('put_score', 0)} / 双买 {sig.get('straddle_score', 0)}",
            f"5m/15m: {met.get('r5', 0):+.2%} / {met.get('r15', 0):+.2%} | 主动买: {met.get('taker_buy_ratio', 0):.0%}",
            f"期权PCR: {flow.get('pcr', 0):.2f} | ATM IV: {flow.get('atm_iv', 0):.1f}%",
            "",
            "触发原因:",
        ]
        for r in sig.get("reasons", []):
            lines.append(f"- {r}")
        lines.extend(["", "━" * 16, "择时信号，不构成交易建议。", ""])
        return "\n".join(lines)

    def _format_sabr_message(self, primary: dict, top3: list[dict]) -> str:
        """格式化 SABR 信号消息为纯文本"""
        lines = []
        lines.append("【期权天眼】BTC/ETH IV曲面异常扫描")
        lines.append("")

        for s in top3:
            direction_cn = "做多" if s.get("direction") == "long" else "做空"
            conf = s.get("confidence", "medium")
            conf_star = {"high": "★★★", "medium": "★★", "low": "★"}.get(conf, "★")
            desc = s.get("description", "")
            # 描述分段：和前端一致
            desc_fmt = desc.replace(": ", ":\n").replace(" + ", "\n").replace("(", "\n(")
            prem = s.get("expected_premium", 0)
            delta = s.get("estimated_delta", 0)
            prem_str = f"{'+' if prem >= 0 else ''}{prem:.5f} {s.get('currency', 'BTC')}"
            delta_str = f"{'+' if delta >= 0 else ''}{delta:.3f}"

            lines.append(f"{direction_cn} {s.get('strategy_type', '?')}  {conf_star}")
            for line in desc_fmt.split("\n"):
                lines.append(f"  {line.strip()}")
            lines.append(f"  权利金: {prem_str}  |  Δ: {delta_str}")
            lines.append("")

        # 尾部
        lines.append("━" * 16)
        lines.append("信号由 SABR IV 曲面偏差扫描生成，仅供研究参考。")
        lines.append("Z值 = (市场IV - SABR期望IV) / 标准差，|Z|>2.0 视为显著偏差。")
        lines.append("策略信号默认都要进行 delta 中性对冲。")
        lines.append("不构成交易建议。")
        lines.append("")

        return "\n".join(lines)

    async def _dispatch(self, body: str, subject: str) -> bool:
        """分发到所有启用的通道"""
        results = []

        # 邮件
        if self.email_cfg.get("enabled"):
            ok, detail = await send_email(
                to_addr=self.email_cfg.get("to_addr", ""),
                subject=subject,
                body=body,
            )
            results.append(("email", ok, detail))
            if ok:
                logger.info(f"邮件推送成功: {subject}")
            else:
                logger.warning(f"邮件推送失败: {detail}")

        # 后续可加电报、微信通道

        return any(ok for _, ok, _ in results)
