"""
推送管理器 — 整合多个推送通道，处理信号格式化/去重/日志

用法:
    notifier = NotificationManager(config)
    await notifier.push_sabr_signals(signals_list)
"""
import logging
import time
from typing import Optional

from notification.store import push_count_today, last_push_ts, mark_pushed, append_log
from notification.channels.email import send_email
from notification.channels.telegram import send_telegram
from notification.channels.wechat import send_wechat

logger = logging.getLogger(__name__)


class NotificationManager:
    """推送管理器"""

    def __init__(self, config: dict):
        self.cfg = config.get("notification", {})
        self.enabled = self.cfg.get("enabled", False)

        # 通道配置
        self.email_cfg = self.cfg.get("email", {})
        self.tg_cfg = self.cfg.get("telegram", {})
        self.wx_cfg = self.cfg.get("wechat", {})

        # 进程内去重缓存（防止 read-check-write 竞争条件导致重复推送）
        self._pushed_this_run: set = set()

    async def push_sabr_signals(self, signals: list[dict]) -> int:
        """推送 SABR 策略信号

        规则：
        - 仅推送 confidence=high（三星）的信号
        - 同一信号（signal_id）每天只推 1 次
        - 合并消息每 8 小时最多推送 1 次（COOLDOWN_HOURS）
        """
        if not self.enabled or not signals:
            return 0

        COOLDOWN_SEC = 8 * 3600  # 8 小时冷却

        # 每 8 小时最多一次合并推送
        batch_key = "sabr_batch"
        last_ts = last_push_ts(batch_key)
        if time.time() - last_ts < COOLDOWN_SEC:
            logger.info(f"SABR 合并推送距上次不足 8 小时，跳过")
            return 0

        # 筛选当天未推过的 high 信号（含进程内缓存防并发）
        new_signals = []
        for s in signals:
            if s.get("confidence") != "high":
                continue
            key = f"sabr_{s.get('id', '')}"
            # 进程内缓存检查（防竞争条件）
            if key in self._pushed_this_run:
                continue
            c = push_count_today(key)
            if c >= 1:
                self._pushed_this_run.add(key)
                continue
            new_signals.append(s)

        # 模糊去重：同一到期日+同策略类型+至少一条腿重合→视为同信号，只保留第一个
        deduped = []
        for s in new_signals:
            s_legs = {l["instrument"] for l in s.get("legs", [])}
            dup = False
            for existing in deduped:
                e_legs = {l["instrument"] for l in existing.get("legs", [])}
                # 同一策略类型 且 共享至少一条腿
                if (s.get("strategy_type") == existing.get("strategy_type") and
                    s_legs & e_legs):
                    dup = True
                    break
            if not dup:
                deduped.append(s)
        if deduped and len(deduped) != len(new_signals):
            logger.info(f"模糊去重: {len(new_signals)} → {len(deduped)} 条信号")
        new_signals = deduped

        if not new_signals:
            return 0

        # 发送前锁定：先标记进程内缓存（防止发送期间的竞争）
        batch_keys = [f"sabr_{s.get('id', '')}" for s in new_signals]
        for k in batch_keys:
            self._pushed_this_run.add(k)

        # 合并成一条消息推送
        body = self._format_sabr_batch(new_signals)
        ok = await self._dispatch(body, "[期权天眼] SABR 信号汇总")
        if ok:
            mark_pushed(batch_key)
            for s in new_signals:
                key = f"sabr_{s.get('id', '')}"
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

        pushed = len(new_signals)
        logger.info(f"SABR 合并推送完成: 批次#{batch_cnt + 1}/3, {pushed} 条新信号")
        return pushed

    def _format_sabr_batch(self, signals: list[dict]) -> str:
        """将所有信号合并为一条消息（保持与单条推送一致的样式的精简版）"""
        lines = ["【期权天眼】SABR IV 曲面异常扫描", ""]

        for i, s in enumerate(signals, 1):
            direction_cn = "做多" if s.get("direction") == "long" else "做空"
            conf = s.get("confidence", "medium")
            conf_star = {"high": "★★★", "medium": "★★", "low": "★"}.get(conf, "★")
            desc = s.get("description", "")
            # 保留完整描述，缩进对齐
            desc_fmt = desc.replace(": ", ":\n").replace(" + ", "\n").replace("(", "\n(")
            prem = s.get("expected_premium", 0)
            delta = s.get("estimated_delta", 0)
            premium_str = f"预期权利金: {'+' if prem >= 0 else ''}{prem:.5f} {s.get('currency', 'BTC')}" if prem != 0 else ""
            delta_str = f"Δ: {'+' if delta >= 0 else ''}{delta:.3f}"

            lines.append(f"{'=' * 4} {direction_cn} {s.get('strategy_type', '?')} {conf_star}")
            for line in desc_fmt.split("\n"):
                lines.append(f"  {line.strip()}")
            info_parts = [p for p in [premium_str, delta_str] if p]
            if info_parts:
                lines.append(f"  {'  |  '.join(info_parts)}")
            lines.append("")

        lines.append("━" * 16)
        lines.append("信号由 SABR IV 曲面偏差扫描生成，仅供研究参考。")
        lines.append("Z值 = (市场IV - SABR期望IV) / 标准差，|Z|>2.0 视为显著偏差。")
        lines.append("策略信号默认都要进行 delta 中性对冲。")
        lines.append("不构成交易建议。")
        lines.append("")
        return "\n".join(lines)

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

        # 电报
        if self.tg_cfg.get("enabled"):
            ok, detail = await send_telegram(body)
            results.append(("telegram", ok, detail))
            if ok:
                logger.info("电报推送成功")
            else:
                logger.warning(f"电报推送失败: {detail}")

        # 微信
        if self.wx_cfg.get("enabled"):
            ok, detail = await send_wechat(subject, body)
            results.append(("wechat", ok, detail))
            if ok:
                logger.info("微信推送成功")
            else:
                logger.warning(f"微信推送失败: {detail}")

        return any(ok for _, ok, _ in results)
