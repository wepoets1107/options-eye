"""
电报推送通道 — 通过 Telegram Bot API 发送消息到冰火岛期权Club群
配置：TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID 从 .env 或环境变量读取
"""
import asyncio
import json
import logging
import urllib.request
import urllib.parse

logger = logging.getLogger(__name__)

# 默认值（从 .env 或环境变量覆盖）
_DEFAULT_TOKEN = "8811187609:AAH_f0KytsCJq20-w_riYfG9JZSebxiAix0"
_DEFAULT_CHAT_ID = "-1004386546323"


def _get_config():
    import os
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", _DEFAULT_TOKEN)
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", _DEFAULT_CHAT_ID)
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
