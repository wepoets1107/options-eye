"""
期权天眼 — 期权链缓存和解析
"""
import time
import logging
from datetime import datetime, timezone
from typing import Optional

from .models import OptionContract, ExpirySlice

logger = logging.getLogger(__name__)

BJT = timezone.utc  # Deribit 使用 UTC


def parse_contract(inst_name: str) -> Optional[dict]:
    """解析合约名 -> {currency, expiration, strike, kind}"""
    try:
        parts = inst_name.split("-")
        if len(parts) >= 4:
            currency = parts[0]
            expiration_str = parts[1]
            strike = float(parts[2])
            kind = "call" if parts[3].upper() == "C" else "put"
            return {
                "currency": currency,
                "expiration_str": expiration_str,
                "strike": strike,
                "kind": kind
            }
    except:
        pass
    return None


def instrument_to_expiry_unix(inst_name: str) -> Optional[int]:
    """从合约名解析到期日 Unix 时间戳"""
    parsed = parse_contract(inst_name)
    if parsed:
        try:
            dt = datetime.strptime(parsed["expiration_str"], "%d%b%y")
            dt = dt.replace(tzinfo=timezone.utc)
            return int(dt.timestamp())
        except:
            pass
    return None


def _iv_to_dec(val) -> float:
    """Deribit mark_iv 是百分比 (34.18=34.18%)，转小数"""
    if val is None:
        return 0.0
    v = float(val)
    if v > 1:  # 百分比格式
        return v / 100.0
    return v


def _find_nearest_strike(contracts: list[OptionContract], target: float) -> Optional[OptionContract]:
    """找行权价最接近 target 的合约"""
    if not contracts:
        return None
    return min(contracts, key=lambda c: abs(c.strike - target))


def build_chain_snapshot(
    raw_data: dict[str, dict],
    index_price: float,
    currency: str,
    min_dte: int = 7,
    max_dte: int = 365,
    min_oi: int = 10
) -> list[ExpirySlice]:
    """
    从原始 book 数据构建期权链快照

    返回按到期日排序的 ExpirySlice 列表
    """
    now = time.time()
    now_utc = datetime.now(timezone.utc)

    # 按到期日分组
    expiry_groups: dict[int, dict] = {}

    for inst_name, data in raw_data.items():
        if not inst_name.startswith(currency):
            continue

        parsed = parse_contract(inst_name)
        if not parsed:
            continue

        # 计算到期日
        try:
            exp_dt = datetime.strptime(parsed["expiration_str"], "%d%b%y")
            exp_dt = exp_dt.replace(tzinfo=timezone.utc)
            exp_ts = int(exp_dt.timestamp())
        except:
            continue

        # DTE 计算
        dte = max(0, (exp_dt - now_utc).days)

        # 过滤
        if dte < min_dte or dte > max_dte:
            continue

        oi = data.get("open_interest", 0) or 0
        if oi < min_oi:
            continue

        # 构建合约
        mark_iv = _iv_to_dec(data.get("mark_iv"))
        bid_iv = _iv_to_dec(data.get("bid_iv"))
        ask_iv = _iv_to_dec(data.get("ask_iv"))

        # 用 moneyness 近似 Delta
        moneyness = parsed["strike"] / index_price if index_price > 0 else 1.0
        if parsed["kind"] == "call":
            approx_delta = max(0, min(1, 1.0 - moneyness * 0.8))
        else:
            approx_delta = max(0, min(1, moneyness * 0.8 - 0.2))

        contract = OptionContract(
            instrument=inst_name,
            currency=currency,
            kind=parsed["kind"],
            strike=parsed["strike"],
            expiration=exp_ts,
            dte=dte,
            mark_iv=mark_iv,
            bid_iv=bid_iv,
            ask_iv=ask_iv,
            mark_price=data.get("mark_price", 0) or 0,
            bid_price=data.get("bid_price", 0) or 0,
            ask_price=data.get("ask_price", 0) or 0,
            delta=approx_delta,
            gamma=0.0,
            vega=0.0,
            theta=0.0,
            open_interest=oi,
            volume_24h=data.get("volume", 0) or 0,
            underlying_price=index_price,
            timestamp=int(now)
        )

        if exp_ts not in expiry_groups:
            expiry_groups[exp_ts] = {
                "expiration": exp_ts,
                "dte": dte,
                "forward": data.get("underlying_price", index_price),
                "calls": [],
                "puts": []
            }

        if parsed["kind"] == "call":
            expiry_groups[exp_ts]["calls"].append(contract)
        else:
            expiry_groups[exp_ts]["puts"].append(contract)

    # 按 DTE 排序，构造结果
    slices = []
    for exp_ts in sorted(expiry_groups.keys()):
        g = expiry_groups[exp_ts]
        calls = sorted(g["calls"], key=lambda c: c.strike)
        puts = sorted(g["puts"], key=lambda c: c.strike)

        # 计算 ATM IV：找行权价最接近 underlying_price 的合约
        atm_iv = 0.0
        nearest_call = _find_nearest_strike(calls, index_price)
        nearest_put = _find_nearest_strike(puts, index_price)
        if nearest_call and nearest_put:
            atm_iv = (nearest_call.mark_iv + nearest_put.mark_iv) / 2
        elif nearest_call:
            atm_iv = nearest_call.mark_iv
        elif nearest_put:
            atm_iv = nearest_put.mark_iv

        # 计算远期近似
        forward = g["forward"]
        if not forward or forward <= 0:
            forward = index_price

        slices.append(ExpirySlice(
            expiration=exp_ts,
            dte=g["dte"],
            currency=currency,
            forward=forward or index_price,
            atm_iv=atm_iv,
            calls=calls,
            puts=puts
        ))

    return slices
