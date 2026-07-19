"""
邮件推送通道 — 使用 QQ Agent Mail CLI (agently-cli.CMD)
不走 SMTP。两段式确认：先获取 token，再带 token 确认发送。
"""
import asyncio
import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

AGENTLY_CMD = "agently-cli.CMD"


def _cleanup(path: Path):
    try:
        if path and path.exists():
            path.unlink()
    except Exception:
        pass


async def send_email(to_addr: str, subject: str, body: str) -> tuple[bool, str]:
    """通过 QQ Agent Mail 发送邮件

    Args:
        to_addr: 收件人地址
        subject: 邮件主题
        body: 正文纯文本

    Returns:
        (成功标志, 详情)
    """
    body_file = None
    try:
        ts = int(asyncio.get_running_loop().time() * 1000)
        body_file = Path.cwd() / f"._oe_body_{ts}.txt"
        body_file.write_text(body, encoding="utf-8")

        async def _run(cmd: list) -> tuple[int, str]:
            p = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            o, e = await asyncio.wait_for(p.communicate(), timeout=25)
            return p.returncode, o.decode(), e.decode()

        # Step 1: 获取确认 token
        cmd1 = [
            AGENTLY_CMD, "message", "+send",
            "--to", to_addr,
            "--subject", subject,
            "--body-file", body_file.name,
            "--body-format", "plain",
        ]
        rc1, out1, err1 = await _run(cmd1)

        if rc1 != 0:
            return False, (err1.strip() or out1.strip())[:300]

        # 解析 token
        try:
            resp = json.loads(out1)
            if not resp.get("ok") or not resp.get("data", {}).get("confirmation_required"):
                return False, "unexpected response"
            token = resp["data"]["confirmation_token"]
        except (json.JSONDecodeError, KeyError, TypeError) as e:
            return False, f"parse token failed: {e}"

        # Step 2: 带 token 确认发送
        cmd2 = cmd1 + ["--confirmation-token", token]
        rc2, out2, err2 = await _run(cmd2)

        if rc2 != 0:
            return False, (err2.strip() or out2.strip())[:300]

        try:
            resp2 = json.loads(out2)
            if resp2.get("ok"):
                logger.info(f"邮件已发送: {to_addr} | {subject}")
                return True, "ok"
            return False, resp2.get("error", {}).get("message", "unknown")
        except json.JSONDecodeError:
            return False, out2[:200]

    except asyncio.TimeoutError:
        return False, "timeout"
    except FileNotFoundError:
        return False, "agently-cli not found"
    except Exception as e:
        return False, str(e)[:200]
    finally:
        if body_file:
            _cleanup(body_file)
