"""
末日期权买方信号评分引擎

从 Binance 永续获取市场数据，从 options-eye 共享状态获取 Deribit 期权数据，
按趋势/动量/资金流/期权盘口/波动率评分，输出 BUY_CALL/BUY_PUT/BUY_STRADDLE 信号。
"""
import json
import logging
import math
import statistics
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from collections import deque

from notification.bnb_client import RollingMarket, ema, safe_float, now_ms

logger = logging.getLogger(__name__)

SH_TZ = timezone.utc  # 简化，不影响评分逻辑

DATA_DIR = Path(__file__).resolve().parents[1] / "data"


def pct(a, b):
    return (a / b - 1) if b else 0.0


def fmt_ts(ts=None):
    dt = datetime.fromtimestamp(ts or time.time(), SH_TZ)
    return dt.strftime("%Y-%m-%d %H:%M:%S")


class ExpiryScorer:
    """末日期权买方信号评分器

    使用方式：
        scorer = ExpiryScorer(market)
        # 每轮评估前更新 Deribit 数据
        scorer.update_deribit(index_price, option_book_list)
        result = scorer.evaluate()  # 返回信号 dict 或 None
    """

    # 评分参数
    CALL_THRESHOLD = 70
    PUT_THRESHOLD = 70
    STRADDLE_THRESHOLD = 75
    OPPOSITE_MAX = 60
    MIN_MINUTES = 30
    MAX_MINUTES = 24 * 60
    MAX_OPTION_SPREAD = 0.12
    MAX_STRADDLE_COST_PCT = 0.010

    STRADDLE_MAX_DIRECTION_SCORE = 62
    STRADDLE_MAX_DIRECTION_GAP = 12
    STRADDLE_MAX_BB_PERCENTILE = 0.35
    STRADDLE_MAX_AMP60 = 0.008
    STRADDLE_MAX_IV_PERCENTILE = 0.65
    STRADDLE_MIN_VOLUME_RATIO = 1.25
    STRADDLE_MIN_FLOW_IMBALANCE = 0.08
    STRADDLE_CONFIRM_VOLUME_RATIO = 1.50
    STRADDLE_CONFIRM_FLOW_IMBALANCE = 0.15
    STRADDLE_MIN_ABS_R5 = 0.0010
    STRADDLE_MIN_ABS_R15 = 0.0020
    STRADDLE_MIN_MOVE_COST_RATIO = 1.60
    STRADDLE_MIN_MINUTES = 45
    STRADDLE_MAX_MINUTES = 240

    def __init__(self, market: RollingMarket):
        self.market = market
        self.index_price = 0.0
        self.options: list = []  # Deribit book_summary 列表
        self.updated_ms = 0
        self.last_signal: Optional[dict] = None
        self._atm_call = None
        self._atm_put = None

    def update_deribit(self, index_price: float, options: list):
        """由 options-eye 主循环每轮调用，传入最新 Deribit 数据"""
        self.index_price = index_price
        self.options = options
        self.updated_ms = now_ms()
        self.market.index_price = index_price

    def fresh(self, max_age_sec=45) -> bool:
        return bool(self.options) and now_ms() - self.updated_ms <= max_age_sec * 1000

    # ---- 工具方法 ----

    @staticmethod
    def parse_instrument(name: str):
        """解析 Deribit 合约名 BTC-28AUG26-74000-C → (到期datetime, strike, C/P)"""
        try:
            parts = name.split("-")
            if len(parts) < 4:
                return None
            expiry_str = "-".join(parts[1:-2])
            strike = float(parts[-2])
            cp = parts[-1]
            exp = datetime.strptime(expiry_str, "%d%b%y").replace(tzinfo=timezone.utc)
            return exp, strike, cp
        except (ValueError, IndexError):
            return None

    def _same_day_options(self):
        """筛选符合到期范围的期权"""
        out = []
        now = datetime.now(timezone.utc)
        for o in self.options:
            name = o.get("instrument_name", "")
            parsed = self.parse_instrument(name)
            if not parsed:
                continue
            exp, strike, cp = parsed
            minutes_left = (exp - now).total_seconds() / 60
            if self.MIN_MINUTES <= minutes_left <= self.MAX_MINUTES:
                o = dict(o)
                o["_expiry"] = exp
                o["_strike"] = strike
                o["_cp"] = cp
                o["_minutes_left"] = minutes_left
                out.append(o)
        return out

    def _option_cost_ok(self, opt) -> tuple:
        bid = safe_float(opt.get("bid_price"))
        ask = safe_float(opt.get("ask_price"))
        if bid <= 0 or ask <= 0 or ask <= bid:
            return False, "无效价差"
        spread = (ask - bid) / ask
        if spread > self.MAX_OPTION_SPREAD:
            return False, f"价差 {spread:.1%} 超限"
        return True, ""

    def _minutes_to_expiry(self):
        opts = self._same_day_options()
        if not opts:
            return 0.0
        return max(o.get("_minutes_left", 0) for o in opts)

    def _choose_option(self, cp: str, delta_low=0.25, delta_high=0.45):
        """选符合条件的期权（成本+delta）"""
        spot = self.index_price or self.market.last_price
        cands = []
        for o in self._same_day_options():
            if o.get("_cp") != cp:
                continue
            greeks = o.get("greeks") or {}
            delta = safe_float(greeks.get("delta"), None) if greeks else None
            if delta is None:
                m = abs(o["_strike"] / spot - 1) if spot else 9
                if m <= 0.025:
                    score = m
                else:
                    continue
            else:
                ad = abs(delta)
                if not (delta_low <= ad <= delta_high):
                    continue
                score = abs(ad - 0.35)
            ok, _ = self._option_cost_ok(o)
            if ok:
                cands.append((score, abs(o["_strike"] - spot), o))
        cands.sort(key=lambda x: (x[0], x[1]))
        return cands[0][2] if cands else None

    def _choose_atm_pair(self, strict=True):
        """选最近月 ATM Call+Put 对"""
        spot = self.index_price or self.market.last_price
        calls, puts = [], []
        for o in self._same_day_options():
            if strict:
                ok, _ = self._option_cost_ok(o)
                if not ok:
                    continue
            item = (abs(o["_strike"] - spot), o)
            if o.get("_cp") == "C":
                calls.append(item)
            elif o.get("_cp") == "P":
                puts.append(item)
        calls.sort(key=lambda x: x[0])
        puts.sort(key=lambda x: x[0])
        if not calls or not puts:
            return None
        return (calls[0][1], puts[0][1])

    # ---- 评分 ----

    def _score_direction(self):
        m = self.market
        closes = [x[1] for x in m.closes]
        price = self.index_price or m.mark_price or m.last_price
        vwap = m.vwap() or price
        ema20 = ema(closes, 20)
        ema60 = ema(closes, 60)
        high120, low120 = m.donchian(120)
        vol_ratio = m.volume_ratio()
        taker_buy_ratio = m.taker_ratio_5m()
        r5 = m.returns(5)
        r15 = m.returns(15)

        call = {"score": 0, "reasons": []}
        put = {"score": 0, "reasons": []}

        # 趋势 (30)
        if price > vwap:
            call["score"] += 8
            call["reasons"].append("价格在VWAP上方")
        if price < vwap:
            put["score"] += 8
            put["reasons"].append("价格在VWAP下方")
        if ema20 and ema60 and ema20 > ema60:
            call["score"] += 8
            call["reasons"].append("EMA20>EMA60")
        if ema20 and ema60 and ema20 < ema60:
            put["score"] += 8
            put["reasons"].append("EMA20<EMA60")
        if high120 and price > high120 * 0.999:
            call["score"] += 8
            call["reasons"].append("接近/突破2小时高点")
        if low120 and price < low120 * 1.001:
            put["score"] += 8
            put["reasons"].append("接近/跌破2小时低点")

        # 动量 (20)
        if r15 > 0.002:
            call["score"] += 6
            call["reasons"].append(f"15m动量 {r15:+.2%}")
        if r15 < -0.001:
            put["score"] += 6
            put["reasons"].append(f"15m动量 {r15:+.2%}")
        if r5 > 0.001:
            call["score"] += 6
            call["reasons"].append(f"5m上涨 {r5:+.2%}")
        if r5 < -0.0005:
            put["score"] += 6
            put["reasons"].append(f"5m下跌 {r5:+.2%}")
        if vol_ratio >= 1.5:
            if r5 > 0:
                call["score"] += 7
                call["reasons"].append(f"放量上攻 {vol_ratio:.1f}x")
            elif r5 < 0:
                put["score"] += 7
                put["reasons"].append(f"放量下跌 {vol_ratio:.1f}x")
        if taker_buy_ratio >= 0.58:
            call["score"] += 7
            call["reasons"].append(f"主动买占比 {taker_buy_ratio:.0%}")
        if taker_buy_ratio <= 0.42:
            put["score"] += 7
            put["reasons"].append(f"主动卖占比 {1-taker_buy_ratio:.0%}")

        # 趋势延续 (30)
        r60 = m.returns(60)
        r120 = m.returns(120)
        if r60 and r60 < -0.005:
            put["score"] += 10
            put["reasons"].append(f"60分钟累计跌幅 {r60:.2%}")
        if r60 and r60 > 0.005:
            call["score"] += 10
            call["reasons"].append(f"60分钟累计涨幅 {r60:+.2%}")
        if r120 and r120 < -0.008:
            put["score"] += 8
            put["reasons"].append(f"120分钟累计跌幅 {r120:.2%}")
        if r120 and r120 > 0.008:
            call["score"] += 8
            call["reasons"].append(f"120分钟累计涨幅 {r120:+.2%}")
        if len(closes) >= 45:
            segs = [closes[i] for i in [-15, -30, -45]]
            seg_ups = sum(1 for i in range(len(segs)-1) if segs[i] > segs[i+1])
            seg_downs = sum(1 for i in range(len(segs)-1) if segs[i] < segs[i+1])
            if seg_downs >= 2:
                put["score"] += 8
                put["reasons"].append("连续3段15分钟走弱")
            if seg_ups >= 2:
                call["score"] += 8
                call["reasons"].append("连续3段15分钟走强")
        if ema60 and price and price < ema60 * 0.992:
            put["score"] += 4
            put["reasons"].append("跌破EMA60超0.8%，趋势确认")
        if ema60 and price and price > ema60 * 1.008:
            call["score"] += 4
            call["reasons"].append("突破EMA60超0.8%，趋势确认")

        call["metrics"] = {"price": price, "vwap": vwap, "r5": r5, "r15": r15,
                           "vol_ratio": vol_ratio, "taker_buy_ratio": taker_buy_ratio}
        put["metrics"] = dict(call["metrics"])
        return call, put

    def _option_flow_score(self, same_day):
        call_vol = put_vol = call_oi = put_oi = 0.0
        call_iv, put_iv, atm_ivs = [], [], []
        spot = self.index_price or self.market.last_price
        for o in same_day:
            vol = safe_float(o.get("volume"))
            oi = safe_float(o.get("open_interest"))
            iv = safe_float(o.get("mark_iv"))
            cp = o.get("_cp")
            if cp == "C":
                call_vol += vol
                call_oi += oi
                call_iv.append(iv)
            elif cp == "P":
                put_vol += vol
                put_oi += oi
                put_iv.append(iv)
            if spot and abs(o.get("_strike", 0) / spot - 1) <= 0.015 and iv > 0:
                atm_ivs.append(iv)
        pcr = put_vol / call_vol if call_vol > 0 else 9.99
        call_avg_iv = statistics.mean([x for x in call_iv if x > 0]) if any(x > 0 for x in call_iv) else 0
        put_avg_iv = statistics.mean([x for x in put_iv if x > 0]) if any(x > 0 for x in put_iv) else 0
        atm_iv = statistics.mean(atm_ivs) if atm_ivs else 0
        return {"call_vol": call_vol, "put_vol": put_vol, "pcr": pcr,
                "call_avg_iv": call_avg_iv, "put_avg_iv": put_avg_iv, "atm_iv": atm_iv}

    def _score_straddle(self, call_score, put_score, flow, pair):
        m = self.market
        score = 0
        reasons = []
        blockers = []
        bbp = m.bb_width_percentile()
        price = self.index_price or m.last_price
        mins_left = self._minutes_to_expiry()
        r5 = m.returns(5)
        r15 = m.returns(15)
        vol_ratio = m.volume_ratio()
        taker_buy_ratio = m.taker_ratio_5m()
        high60, low60 = m.donchian(60)
        high120, low120 = m.donchian(120)
        amp60 = (high60 - low60) / price if high60 and low60 and price else None
        near_key = bool(price and high120 and low120 and
                        (price > high120 * 0.9985 or price < low120 * 1.0015))
        flow_imbalance = abs(taker_buy_ratio - 0.5)
        catalyst = near_key or vol_ratio >= self.STRADDLE_MIN_VOLUME_RATIO or flow_imbalance >= self.STRADDLE_MIN_FLOW_IMBALANCE
        momentum_confirm = abs(r5) >= self.STRADDLE_MIN_ABS_R5 or abs(r15) >= self.STRADDLE_MIN_ABS_R15
        volume_confirm = vol_ratio >= self.STRADDLE_CONFIRM_VOLUME_RATIO
        key_flow_confirm = near_key and flow_imbalance >= self.STRADDLE_CONFIRM_FLOW_IMBALANCE
        expansion_ok = momentum_confirm or volume_confirm or key_flow_confirm

        total_premium_pct = None
        expected_move_pct = None
        move_cost_ratio = None
        if pair and price:
            c, p = pair
            total_premium_pct = safe_float(c.get("ask_price")) + safe_float(p.get("ask_price"))
            time_factor = max(0.65, min(1.35, math.sqrt(max(mins_left, 1) / 120)))
            expected_move_pct = max(
                (amp60 or 0) * 1.6,
                abs(r15) * 2.5,
                abs(r5) * 5.0,
            ) * time_factor
            move_cost_ratio = expected_move_pct / total_premium_pct if total_premium_pct > 0 else None

        neutral_ok = (
            call_score < self.CALL_THRESHOLD and put_score < self.PUT_THRESHOLD
            and max(call_score, put_score) <= self.STRADDLE_MAX_DIRECTION_SCORE
            and abs(call_score - put_score) <= self.STRADDLE_MAX_DIRECTION_GAP
        )
        compression_ok = (bbp is not None and bbp <= self.STRADDLE_MAX_BB_PERCENTILE) or \
                         (amp60 is not None and amp60 <= self.STRADDLE_MAX_AMP60)
        time_ok = self.STRADDLE_MIN_MINUTES <= mins_left <= self.STRADDLE_MAX_MINUTES
        cost_ok = total_premium_pct is not None and total_premium_pct <= self.MAX_STRADDLE_COST_PCT
        expectancy_ok = move_cost_ratio is not None and move_cost_ratio >= self.STRADDLE_MIN_MOVE_COST_RATIO

        if not neutral_ok:
            blockers.append("方向分不够中性")
        if not compression_ok:
            blockers.append("缺少波动压缩")
        if not catalyst:
            blockers.append("缺少突破催化")
        if not expansion_ok:
            blockers.append("缺少扩波确认")
        if not time_ok:
            blockers.append("剩余到期不适合双买")
        if not cost_ok:
            blockers.append(f"双买成本偏贵 {total_premium_pct:.2%}" if total_premium_pct else "无有效双买成本")
        if not expectancy_ok:
            blockers.append(f"预估波动/成本不足 {move_cost_ratio:.1f}x" if move_cost_ratio else "无法估算波动成本比")

        if neutral_ok:
            score += 10
            reasons.append("方向不明，单边信号未确认")
        if bbp is not None and bbp <= self.STRADDLE_MAX_BB_PERCENTILE:
            add = 30 if bbp <= 0.25 else 22
            score += add
            reasons.append(f"布林带宽低分位 {bbp:.0%}")
        if amp60 is not None and amp60 <= self.STRADDLE_MAX_AMP60:
            score += 18
            reasons.append(f"1小时振幅压缩 {amp60:.2%}")
        if catalyst:
            score += 16
            if near_key:
                reasons.append("贴近2小时关键位，可能突破")
            elif vol_ratio >= self.STRADDLE_MIN_VOLUME_RATIO:
                reasons.append(f"成交放大 {vol_ratio:.1f}x")
            else:
                reasons.append(f"资金流偏离 {taker_buy_ratio:.0%}")
        if expansion_ok:
            score += 14
            if momentum_confirm:
                reasons.append(f"短线开始扩波 5m/15m {r5:+.2%}/{r15:+.2%}")
            elif volume_confirm:
                reasons.append(f"成交确认扩波 {vol_ratio:.1f}x")
            else:
                reasons.append(f"关键位资金流确认 {taker_buy_ratio:.0%}")
        if time_ok:
            score += 8
            reasons.append("剩余到期时间适合双买")
        if cost_ok:
            score += 12
            reasons.append(f"双买成本 {total_premium_pct:.2%}")
        if expectancy_ok:
            score += 18
            reasons.append(f"预估波动/成本 {move_cost_ratio:.1f}x")

        gated = neutral_ok and compression_ok and catalyst and expansion_ok and time_ok and cost_ok and expectancy_ok
        raw_score = max(0, score)
        if not gated:
            strongest_direction = max(call_score, put_score)
            score = min(raw_score, self.STRADDLE_THRESHOLD - 1, strongest_direction - 5)

        return {
            "score": max(0, score),
            "reasons": reasons if gated else reasons + ["未触发：" + "、".join(blockers[:3])],
            "blocked": not gated,
            "blockers": blockers,
            "metrics": {"raw_score": raw_score, "amp60": amp60, "total_premium_pct": total_premium_pct,
                        "expected_move_pct": expected_move_pct, "move_cost_ratio": move_cost_ratio},
        }

    def evaluate(self) -> Optional[dict]:
        """主评估方法，返回信号 dict 或 None"""
        if not self.fresh():
            return None

        min_closes = 65
        min_trades = 20
        if len(self.market.closes) < min_closes or len(self.market.trades) < min_trades:
            return None

        same_day = self._same_day_options()
        if not same_day:
            return None

        mins_left = self._minutes_to_expiry()
        if mins_left < self.MIN_MINUTES:
            return None

        call, put = self._score_direction()
        flow = self._option_flow_score(same_day)

        # 期权盘口加分
        if flow["pcr"] < 0.75:
            call["score"] += 10
            call["reasons"].append(f"PCR偏低 {flow['pcr']:.2f}")
        if flow["pcr"] > 1.25 and flow["pcr"] < 9:
            put["score"] += 10
            put["reasons"].append(f"PCR偏高 {flow['pcr']:.2f}")
        if flow["call_avg_iv"] > flow["put_avg_iv"] * 1.03 and flow["call_avg_iv"] > 0:
            call["score"] += 8
            call["reasons"].append("Call IV相对抬升")
        if flow["put_avg_iv"] > flow["call_avg_iv"] * 1.03 and flow["put_avg_iv"] > 0:
            put["score"] += 8
            put["reasons"].append("Put IV相对抬升")

        # 选合约
        call_opt = self._choose_option("C")
        put_opt = self._choose_option("P")
        atm_pair = self._choose_atm_pair(strict=False)
        self._atm_call, self._atm_put = atm_pair if atm_pair else (None, None)

        if call_opt:
            call["score"] += 10
            call["reasons"].append(f"Call成本合格 {call_opt['instrument_name']}")
        if put_opt:
            put["score"] += 10
            put["reasons"].append(f"Put成本合格 {put_opt['instrument_name']}")

        pair = self._choose_atm_pair(strict=True)
        straddle = self._score_straddle(call["score"], put["score"], flow, pair)

        call_final = max(0, min(100, call["score"]))
        put_final = max(0, min(100, put["score"]))
        straddle_final = max(0, min(100, straddle["score"]))

        # 决策
        signal = None
        contract = None
        confidence = 0
        reasons = []

        if call_final >= self.CALL_THRESHOLD and put_final < self.OPPOSITE_MAX and (call_opt or self._atm_call):
            signal = "BUY_CALL"
            contract = (self._atm_call or call_opt).get("instrument_name")
            confidence = min(100, call_final)
            reasons = call["reasons"]
        elif put_final >= self.PUT_THRESHOLD and call_final < self.OPPOSITE_MAX and (put_opt or self._atm_put):
            signal = "BUY_PUT"
            contract = (self._atm_put or put_opt).get("instrument_name")
            confidence = min(100, put_final)
            reasons = put["reasons"]
        elif straddle_final >= self.STRADDLE_THRESHOLD and not straddle.get("blocked") and pair:
            signal = "BUY_STRADDLE"
            contract = f"{pair[0].get('instrument_name')} + {pair[1].get('instrument_name')}"
            confidence = min(100, straddle_final)
            reasons = straddle["reasons"]

        if not signal:
            return None

        result = {
            "signal": signal,
            "confidence": confidence,
            "contract": contract,
            "minutes_left": mins_left,
            "call_score": call_final,
            "put_score": put_final,
            "straddle_score": straddle_final,
            "flow": flow,
            "metrics": call["metrics"],
            "reasons": reasons[:8],
        }
        self.last_signal = result
        return result

    def format_message(self, sig: dict) -> str:
        """格式化信号为纯文本消息"""
        met = sig["metrics"]
        flow = sig["flow"]
        signal_cn = {"BUY_CALL": "买入Call", "BUY_PUT": "买入Put", "BUY_STRADDLE": "双买"}.get(sig["signal"], sig["signal"])
        lines = [
            f"【末日期权信号】BTC {signal_cn}",
            "",
            f"信号强度: {sig['confidence']}",
            f"合约: {sig['contract']}",
            f"剩余到期: {sig['minutes_left']:.0f}分钟",
            f"现价: {met['price']:,.1f} | VWAP: {met['vwap']:,.1f}",
            f"评分: Call {sig['call_score']} / Put {sig['put_score']} / 双买 {sig['straddle_score']}",
            f"5m/15m: {met['r5']:+.2%} / {met['r15']:+.2%} | 主动买: {met['taker_buy_ratio']:.0%}",
            f"期权PCR: {flow['pcr']:.2f} | ATM IV: {flow['atm_iv']:.1f}%",
            "",
            "触发原因:",
        ]
        for r in sig["reasons"]:
            lines.append(f"- {r}")
        lines.extend(["", "━" * 16, "择时信号，不构成交易建议。", ""])
        return "\n".join(lines)
