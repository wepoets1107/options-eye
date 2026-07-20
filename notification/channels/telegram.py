"""电报推送通道（占位，待接入）"""
import logging

logger = logging.getLogger(__name__)


async def send_telegram(body: str) -> tuple[bool, str]:
    logger.info("电报推送通道未配置，跳过")
    return False, "not_configured"
