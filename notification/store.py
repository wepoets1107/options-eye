"""
推送状态持久化 — 已推信号去重 / 冷却 / 每日配额

存储路径：options-eye/data/
- pushed_signals.json：{ "YYYY-MM-DD_key": [timestamp, ...] } 支持按次计数
- notifications.jsonl：推送历史日志
"""
import json
import logging
import time
from pathlib import Path

logger = logging.getLogger(__name__)

DATA_DIR = Path(__file__).resolve().parents[1] / "data"
PUSHED_FILE = DATA_DIR / "pushed_signals.json"
LOG_FILE = DATA_DIR / "notifications.jsonl"


def _ensure_dir():
    DATA_DIR.mkdir(parents=True, exist_ok=True)


def _load_pushed() -> dict:
    _ensure_dir()
    if PUSHED_FILE.exists():
        try:
            return json.loads(PUSHED_FILE.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning(f"读取推送状态失败: {e}")
    return {}


def _save_pushed(data: dict):
    _ensure_dir()
    try:
        PUSHED_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        logger.warning(f"写入推送状态失败: {e}")


def push_count_today(key: str) -> int:
    """当天某类信号已推送次数"""
    today = time.strftime("%Y-%m-%d")
    full_key = f"{today}_{key}"
    pushed = _load_pushed()
    records = pushed.get(full_key, [])
    if isinstance(records, list):
        return len(records)
    # 兼容旧格式（单个 timestamp）
    return 1 if records else 0


def mark_pushed(key: str):
    """标记某类信号已推送一次"""
    today = time.strftime("%Y-%m-%d")
    full_key = f"{today}_{key}"
    pushed = _load_pushed()
    records = pushed.get(full_key, [])
    if not isinstance(records, list):
        records = [records] if records else []
    records.append(int(time.time()))
    pushed[full_key] = records
    _save_pushed(pushed)
    logger.info(f"已标记推送: {full_key} (累计 {len(records)} 次)")


def last_push_ts(key: str) -> float:
    """某类信号最近一次推送的时间戳，0 表示从未推过"""
    today = time.strftime("%Y-%m-%d")
    full_key = f"{today}_{key}"
    pushed = _load_pushed()
    records = pushed.get(full_key, [])
    if isinstance(records, list) and records:
        return float(records[-1])
    if isinstance(records, (int, float)):
        return float(records)
    return 0.0


def append_log(entry: dict):
    """追加推送日志"""
    _ensure_dir()
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception as e:
        logger.warning(f"写入推送日志失败: {e}")
