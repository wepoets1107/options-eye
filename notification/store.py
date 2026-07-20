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
LEGS_FILE = DATA_DIR / "pushed_legs.json"   # 已推信号的策略+腿信息（用于模糊去重）
LOG_FILE = DATA_DIR / "notifications.jsonl"
RETENTION_DAYS = 7  # 推送记录保留天数


def _ensure_dir():
    DATA_DIR.mkdir(parents=True, exist_ok=True)


def _cleanup_old(data: dict) -> int:
    """清理超过 RETENTION_DAYS 天的记录"""
    cutoff = time.time() - RETENTION_DAYS * 86400
    cutoff_date = time.strftime("%Y-%m-%d", time.gmtime(cutoff))
    removed = 0
    for key in list(data.keys()):
        # 键格式：YYYY-MM-DD_xxx 或 YYYY-MM-DD
        date_part = key[:10]
        if date_part < cutoff_date:
            del data[key]
            removed += 1
    return removed


def _load_pushed() -> dict:
    _ensure_dir()
    if PUSHED_FILE.exists():
        try:
            data = json.loads(PUSHED_FILE.read_text(encoding="utf-8"))
            removed = _cleanup_old(data)
            if removed:
                _save_pushed(data)
                logger.info(f"清理了 {removed} 条超过 {RETENTION_DAYS} 天的推送记录")
            return data
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


# ===== 模糊去重：存储/读取已推信号腿信息 =====


def save_pushed_legs(strategy_type: str, legs: set[str]):
    """记录已推信号的策略类型和腿列表"""
    _ensure_dir()
    today = time.strftime("%Y-%m-%d")
    try:
        data = {}
        if LEGS_FILE.exists():
            data = json.loads(LEGS_FILE.read_text(encoding="utf-8"))
            _cleanup_old(data)
        day = data.get(today, [])
        day.append({"strategy": strategy_type, "legs": sorted(legs)})
        data[today] = day
        LEGS_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        logger.warning(f"保存腿信息失败: {e}")


def has_conflicting_legs(strategy_type: str, legs: set[str]) -> bool:
    """检查今天是否已推过同策略类型且共享至少一条腿的信号"""
    _ensure_dir()
    today = time.strftime("%Y-%m-%d")
    try:
        if not LEGS_FILE.exists():
            return False
        data = json.loads(LEGS_FILE.read_text(encoding="utf-8"))
        _cleanup_old(data)
        for entry in data.get(today, []):
            if entry.get("strategy") == strategy_type:
                pushed_legs = set(entry.get("legs", []))
                if legs & pushed_legs:  # 共享至少一条腿
                    return True
    except Exception:
        pass
    return False
