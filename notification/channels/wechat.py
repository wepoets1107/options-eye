"""
微信推送通道 — 通过 wx-send 脚本（邮件推送，岛主微信收到通知）
"""
import asyncio
import logging

logger = logging.getLogger(__name__)

WX_SEND_CMD = "/usr/local/bin/wx-send"


async def send_wechat(subject: str, body: str) -> tuple[bool, str]:
    """通过 wx-send 推送微信

    Args:
        subject: 标题（微信通知显示）
        body: 消息正文

    Returns:
        (成功标志, 详情)
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            WX_SEND_CMD, subject, body,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        o, e = await asyncio.wait_for(proc.communicate(), timeout=15)
        stdout = o.decode().strip()
        stderr = e.decode().strip()

        if proc.returncode == 0:
            logger.info(f"微信推送成功: {subject}")
            return True, stdout
        else:
            return False, stderr or stdout

    except asyncio.TimeoutError:
        return False, "wx-send timeout"
    except FileNotFoundError:
        return False, "wx-send not found"
    except Exception as ex:
        return False, str(ex)[:200]
