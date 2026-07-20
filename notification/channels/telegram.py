"""
电报推送通道 — 通过 Telegram Bot API 发送消息到冰火岛期权Club群
凭据来源（优先级）：环境变量 TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID > 注入凭据 > 无
注入方式：NotificationManager 初始化时调用 set_credentials(token, chat_id)
真实 bot_token/chat_id 仅存在于本地 config.yaml（gitignored），不进入代码仓库。
"""
import asyncio
import json
import logging
import urllib.request
import urllib.parse

logger = logging.getLogger(__name__)

# 凭据由 set_credentials 注入（来自本地 config.yaml），绝不在源码里硬编码
_BOT_TOKEN = ""
_CHAT_ID = ""


def set_credentials(bot_token: str, chat_id: str) -> None:
    """由 NotificationManager 从本地 config 注入凭据（config.yaml 不入库）"""
    global _BOT_TOKEN, _CHAT_ID
    _BOT_TOKEN = bot_token or ""
    _CHAT_ID = chat_id or ""


def _get_config():
    import os
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN") or _BOT_TOKEN
    chat_id = os.environ.get("TELEGRAM_CHAT_ID") or _CHAT_ID
    return bot_token, chat_id


async def send_telegram(body: str) -> tuple[bool, str]:
    """发送纯文本消息到电报群"""
    bot_token, chat_id = _get_config()
    if not bot_token or not chat_id:
        return False, "TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not set"

    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    params = {
        "chat_id": chat_id,
        "text": body,
        "disable_web_page_preview": "true",
    }
    data = urllib.parse.urlencode(params).encode("utf-8")

    def _send():
        req = urllib.request.Request(url, data=data)
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read())

    try:
        result = await asyncio.to_thread(_send)
        if result.get("ok"):
            logger.info(f"电报推送成功 (message_id: {result['result']['message_id']})")
            return True, "ok"
        else:
            logger.warning(f"电报推送失败: {result}")
            return False, str(result)
    except Exception as e:
        logger.warning(f"电报推送异常: {e}")
        return False, str(e)[:200]


async def send_telegram_html_file(file_path: str) -> tuple[bool, str]:
    """发送 HTML 文件到电报群（作为文档）"""
    bot_token, chat_id = _get_config()
    import io

    boundary = "----FormBoundary7MA4YWxkTrZu0gW"

    def _send():
        with open(file_path, "rb") as f:
            file_content = f.read()
        filename = file_path.split("/")[-1].split("\\")[-1]
        body = io.BytesIO()
        body.write(f"--{boundary}\r\n".encode())
        body.write(f'Content-Disposition: form-data; name="chat_id"\r\n\r\n'.encode())
        body.write(f"{chat_id}\r\n".encode())
        body.write(f"--{boundary}\r\n".encode())
        body.write(f'Content-Disposition: form-data; name="document"; filename="{filename}"\r\n'.encode())
        body.write(b"Content-Type: text/html\r\n\r\n")
        body.write(file_content)
        body.write("\r\n".encode())
        body.write(f"--{boundary}--\r\n".encode())

        url = f"https://api.telegram.org/bot{bot_token}/sendDocument"
        req = urllib.request.Request(url, data=body.getvalue())
        req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())

    try:
        result = await asyncio.to_thread(_send)
        if result.get("ok"):
            logger.info(f"电报文件推送成功")
            return True, "ok"
        return False, str(result)
    except Exception as e:
        return False, str(e)[:200]
