"""
期权天眼 — FastAPI Web 工作台后端
"""
import asyncio
import json
import logging
import os
import time
import yaml
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi import Request
from pydantic import BaseModel

logger = logging.getLogger(__name__)

app = FastAPI(title="期权天眼")

# .env 文件路径（不在 git 跟踪，存 Deribit 交易凭证自动恢复）
ENV_PATH = Path(__file__).resolve().parent.parent / ".env"


def _load_env_simple(path):
    """简易 .env 解析器，不依赖 python-dotenv"""
    env = {}
    if not path.exists():
        return env
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def _save_env_simple(path, data):
    """写入 .env 文件"""
    with open(path, "w", encoding="utf-8") as f:
        for k, v in data.items():
            f.write(f'{k}={v}\n')

# 全局状态（由 main.py 注入）
state = {
    "config": None,
    "deribit_ws": None,
    "latest_slices": [],
    "latest_sabr_params": {},
    "latest_deviations": [],
    "latest_signals": [],
    "last_update": 0,
    "is_running": False,
    "trader": None,
    "client_id": None,
    "client_secret": None,
    "position_manager": None,
    "gl_hedge": None,
    "signal_status_overrides": {},   # signal_id -> confirmed/ignored（主循环重建 latest_signals 时回填）
    "runtime_params": {},            # 前端动态参数（min_dte/delta_min/delta_max/z_threshold/min_oi），覆盖 config 默认
    "version": "",                   # 由 main.py 注入（APP_VERSION）
    "w1_state": {},                  # W1 VRP 反转信号状态（strategy/w1_vrp.py 写入）
    "w1_rnd_history": {},            # RND 历史快照（w1_loop 维护，落盘 data/w1_rnd_history.json）
}


from pydantic import BaseModel


class ExecuteRequest(BaseModel):
    signal_id: str
    amount: int = 1  # 仓位倍数，默认 1 手


class TradeCredentials(BaseModel):
    client_id: str
    client_secret: str


class GlHedgeCredentials(BaseModel):
    email: str
    pwd: str


@app.on_event("startup")
async def startup():
    """启动时自动加载凭证"""
    # 挂载静态文件
    static_dir = Path(__file__).parent / "static"
    static_dir.mkdir(exist_ok=True)
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    # 尝试从 .env 自动加载交易凭证
    try:
        env = _load_env_simple(ENV_PATH)
        cid = env.get("DERIBIT_CLIENT_ID", "").strip()
        sec = env.get("DERIBIT_CLIENT_SECRET", "").strip()
        if cid and sec:
            from execution.deribit_trader import DeribitTrader
            trader = DeribitTrader(cid, sec, testnet=True)
            await trader.connect()
            state["trader"] = trader
            state["client_id"] = cid
            logger.info("已从 .env 自动加载交易凭证并连接")
    except Exception as e:
        logger.warning(f".env 自动加载交易凭证失败（可忽略，手动填写即可）: {e}")

    # 尝试从 .env 自动登录格致对冲
    try:
        geli_email = env.get("GELI_EMAIL", "").strip()
        geli_pwd = env.get("GELI_PASSWORD", "").strip()
        if geli_email and geli_pwd:
            from execution.greeks_live_hedge import GreeksLiveHedge
            gl = GreeksLiveHedge(geli_email, geli_pwd)
            ok = await gl.login()
            if ok:
                await gl.get_account_id()
                state["gl_hedge"] = gl
                logger.info(f"已从 .env 自动登录格致对冲，账户: {gl.account_id}")
                # 自动启动 BTC 和 ETH 的 delta 对冲
                for cur in ("BTC", "ETH"):
                    try:
                        band = 0.05 if cur == "BTC" else 0.5
                        await gl.start_delta_hedge(
                            currency=cur,
                            max_positive=band,
                            max_negative=band
                        )
                        logger.info(f"格致 delta 对冲已自动启动: {cur} band=±{band}")
                    except Exception as e:
                        logger.warning(f"格致自动启动 {cur} 对冲失败: {e}")
    except Exception as e:
        logger.warning(f".env 自动登录格致失败（可忽略，手动登录即可）: {e}")


@app.get("/", response_class=HTMLResponse)
async def index():
    html_path = Path(__file__).parent / "templates" / "index.html"
    return HTMLResponse(html_path.read_text(encoding="utf-8"))


@app.get("/api/status")
async def api_status():
    """获取整体状态"""
    s = state
    return {
        "running": s["is_running"],
        "last_update": s["last_update"],
        "last_ticker_ts": int(s["deribit_ws"].last_ticker_ts) if s.get("deribit_ws") else 0,
        "slices": len(s["latest_slices"]),
        "sabr_params": len(s["latest_sabr_params"]),
        "deviations": len(s["latest_deviations"]),
        "signals": len(s["latest_signals"]),
        "version": s.get("version", ""),
        "trader_ready": s.get("trader") is not None,
        "gl_ready": s.get("gl_hedge") is not None,
    }


@app.get("/api/config")
async def api_config():
    """获取当前配置"""
    return state.get("config", {})


@app.get("/api/slices")
async def api_slices():
    """获取期权链切片数据（用于热力图）"""
    slices = state["latest_slices"]
    sabr = state["latest_sabr_params"]
    result = []
    for s in slices:
        p = sabr.get(f"{s.currency}_{s.expiration}")
        currency = s.currency

        calls_data = [
            {"strike": c.strike, "iv": c.mark_iv, "delta": c.delta, "oi": c.open_interest,
             "instrument": c.instrument, "bid": c.bid_price, "ask": c.ask_price}
            for c in s.calls
        ]
        puts_data = [
            {"strike": p.strike, "iv": p.mark_iv, "delta": p.delta, "oi": p.open_interest,
             "instrument": p.instrument, "bid": p.bid_price, "ask": p.ask_price}
            for p in s.puts
        ]
        result.append({
            "expiration": s.expiration,
            "dte": s.dte,
            "currency": currency,
            "atm_iv": s.atm_iv,
            "forward": s.forward,
            "sabr": {
                "alpha": p.alpha, "rho": p.rho, "nu": p.nu, "rmse": p.rmse
            } if p else None,
            "calls": calls_data,
            "puts": puts_data,
        })
    return {"slices": result, "update_time": state["last_update"]}


@app.get("/api/deviations")
async def api_deviations():
    """获取偏差信号（用于异常标注）"""
    devs = state["latest_deviations"]
    return {
        "deviations": [
            {
                "instrument": d.instrument,
                "currency": d.currency,
                "expiration": d.expiration,
                "kind": d.kind,
                "strike": d.strike,
                "dte": d.dte,
                "market_iv": d.market_iv,
                "expected_iv": d.sabr_expected_iv,
                "deviation_pt": d.deviation_pt,
                "z_score": d.z_score,
                "delta": d.delta,
                "spread_filter_pass": d.spread_filter_pass,
                "oi_filter_pass": d.oi_filter_pass,
            }
            for d in devs
        ]
    }


@app.get("/api/signals")
async def api_signals():
    """获取策略信号（合并确认/忽略状态，避免主循环重建后丢失）"""
    sigs = state["latest_signals"]
    overrides = state["signal_status_overrides"]
    # 清理：只保留当前活跃信号 id 的 override，避免长期运行积累废弃 id
    if sigs and len(overrides) > 50:
        active_ids = {s["id"] for s in sigs}
        state["signal_status_overrides"] = {k: v for k, v in overrides.items() if k in active_ids}
        overrides = state["signal_status_overrides"]
    for s in sigs:
        ov = overrides.get(s["id"])
        if ov:
            s["status"] = ov
    return {"signals": sigs}


@app.get("/api/expiry")
async def api_expiry():
    """获取末日期权买方信号评分快照（每轮评估都更新，未触发也返回实时分数）"""
    return state.get("expiry_state", {})


@app.get("/api/w1_state")
async def api_w1_state():
    """获取 VRP 反转信号(W1 Wasserstein)状态：RND 元信息、各 tenor 的 W1/jump-trend、触发状态"""
    return state.get("w1_state", {})


@app.post("/api/credentials")
async def api_credentials(cred: TradeCredentials):
    """设置 Deribit 交易凭证（同时保存到 .env，重启后自动加载）"""
    try:
        from execution.deribit_trader import DeribitTrader
        trader = DeribitTrader(cred.client_id, cred.client_secret, testnet=True)
        await trader.connect()
        state["trader"] = trader
        state["client_id"] = cred.client_id
        # 保存到 .env 以便重启后自动恢复
        _save_env_simple(ENV_PATH, {
            "DERIBIT_CLIENT_ID": cred.client_id,
            "DERIBIT_CLIENT_SECRET": cred.client_secret,
        })
        logger.info("交易凭证已保存到 .env")
        return {"status": "ok", "message": "Trader connected. Credentials saved to .env"}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@app.post("/api/signal/{signal_id}/confirm")
async def api_confirm_signal(signal_id: str):
    """确认信号（标记为待执行）"""
    state["signal_status_overrides"][signal_id] = "confirmed"
    for s in state["latest_signals"]:
        if s["id"] == signal_id:
            s["status"] = "confirmed"
            return {"status": "ok"}
    return {"status": "ok"}


@app.post("/api/signal/{signal_id}/ignore")
async def api_ignore_signal(signal_id: str):
    """忽略信号"""
    state["signal_status_overrides"][signal_id] = "ignored"
    for s in state["latest_signals"]:
        if s["id"] == signal_id:
            s["status"] = "ignored"
            return {"status": "ok"}
    return {"status": "ok"}


@app.post("/api/execute")
async def api_execute(req: ExecuteRequest):
    """执行信号并记录持仓"""
    trader = state.get("trader")
    if not trader:
        return {"status": "error", "message": "Trader not initialized. Set credentials first."}

    signal = None
    for s in state["latest_signals"]:
        if s["id"] == req.signal_id:
            signal = s
            break
    if not signal:
        return {"status": "error", "message": f"Signal {req.signal_id} not found"}

    # 校验：必须先确认（让后端确认环节真正生效）
    status = state["signal_status_overrides"].get(req.signal_id, signal.get("status", "pending"))
    if status != "confirmed":
        return {"status": "error", "message": "信号未确认，请先点击确认再执行"}

    try:
        # 按仓位倍数放大 legs 和对冲量
        mult = max(1, req.amount)
        exec_signal = {
            "id": signal["id"],
            "currency": signal["currency"],
            "legs": [
                {**leg, "amount": leg.get("amount", 1) * mult}
                for leg in signal.get("legs", [])
            ],
            "hedge_instrument": signal.get("hedge_instrument", ""),
            "hedge_direction": signal.get("hedge_direction", ""),
            "hedge_amount": (signal.get("hedge_amount", 0) or 0) * mult,
        }
        results = await trader.execute_signal(exec_signal)
        for s in state["latest_signals"]:
            if s["id"] == req.signal_id:
                s["status"] = "executed"
                s["result"] = results
                break
        pm = state.get("position_manager")
        if pm:
            from data.models import Signal as SignalModel
            sig = SignalModel(id=signal["id"], currency=signal["currency"],
                strategy_type=signal["strategy_type"], direction=signal["direction"],
                confidence=signal["confidence"], description=signal["description"],
                legs=exec_signal["legs"], hedge_instrument=exec_signal.get("hedge_instrument",""),
                hedge_direction=exec_signal.get("hedge_direction",""),
                hedge_amount=exec_signal.get("hedge_amount",0),
                expected_premium=(signal.get("expected_premium",0) or 0) * mult,
                estimated_delta=(signal.get("estimated_delta",0) or 0) * mult,
                deviations=[], created_at=int(time.time()))
            pm.add_position(sig)
            # 回写实际成交的对冲量
            hedge_inst = signal.get("hedge_instrument", "")
            pos = pm.positions.get(signal["id"])
            if pos and hedge_inst:
                hr = results.get(hedge_inst, {})
                if isinstance(hr, dict) and hr.get("status") == "hedge_ok":
                    pos.hedge_amount = float(hr.get("amount", 0))
                else:
                    pos.hedge_amount = 0.0
            # 如果格致已登录，自动启动格致 delta 对冲（永续合约太小则走格致专业对冲）
            gl = state.get("gl_hedge")
            if gl and signal.get("currency"):
                try:
                    cur = signal["currency"]
                    gs = await gl.get_hedge_status(currency=cur)
                    if gs.get("status") == "ok" and not gs.get("is_run"):
                        # 未启动才启动，避免重复
                        await gl.start_delta_hedge(currency=cur)
                        logger.info(f"格致对冲已自动启动: {cur}")
                        if pos:
                            pos.hedge_active = True
                    elif gs.get("is_run"):
                        if pos:
                            pos.hedge_active = True
                except Exception as e:
                    logger.warning(f"自动启动格致对冲失败: {e}")
        return {"status": "ok", "results": results}
    except Exception as e:
        logger.error(f"Execute failed: {e}")
        return {"status": "error", "message": str(e)}


@app.get("/api/positions")
async def api_positions():
    """从交易所拉取持仓，含 Deribit 账户总 delta"""
    pm = state.get("position_manager")
    trader = state.get("trader")
    if not pm or not trader:
        return {"positions": [], "summary": {}}
    positions = await pm.refresh_from_exchange(trader)
    pos_dicts = [p.to_dict() for p in positions]
    summary = pm.get_net_position(positions)
    # 从 Deribit 获取账户总 delta
    try:
        acc = await trader._send("private/get_account_summary", {
            "currency": "BTC", "extended": True
        })
        if isinstance(acc, dict):
            summary["delta_total"] = round(acc.get("delta_total", 0), 4)
            summary["options_delta"] = round(acc.get("options_delta", 0), 4)
    except Exception as e:
        logger.warning(f"获取账户总 delta 失败: {e}")
    # 追加永续合约持仓
    try:
        perp = await trader._send("private/get_positions", {
            "currency": "BTC", "kind": "future"
        })
        perp_items = perp if isinstance(perp, list) else perp.get("result", [])
        for item in perp_items:
            inst = item.get("instrument_name", "")
            size = item.get("size", 0)
            if size == 0:
                continue
            pos_dicts.append({
                "instrument": inst,
                "kind": "future",
                "size": size,
                "delta": round(item.get("delta", 0), 4),
                "mark_price": item.get("mark_price", 0),
                "unrealized_pnl": round(item.get("floating_profit_loss", 0), 6),
                "avg_price": item.get("average_price", 0),
                "signal_id": "",
                "strategy_type": "delta_hedge",
                "direction": "short" if size < 0 else "long",
                "currency": "BTC",
                "description": "格致 delta 对冲（永续）",
                "status": "open",
                "executed_at": int(time.time()),
                "last_update": int(time.time()),
            })
    except Exception as e:
        logger.warning(f"获取永续持仓失败: {e}")
    return {"positions": pos_dicts, "summary": summary}


@app.post("/api/position/{signal_id}/close")
async def api_close_position(signal_id: str):
    """平仓：有 signal_id 按策略腿平仓，否则按 instrument 直接平"""
    pm = state.get("position_manager")
    trader = state.get("trader")
    results = {}
    currency = "BTC"

    if pm and trader:
        signal = pm._signal_map.get(signal_id)
        if signal:
            currency = signal.currency
            for leg in signal.legs:
                close_dir = "sell" if leg.get("direction") == "buy" else "buy"
                try:
                    r = await trader.place_order(
                        instrument=leg["instrument"],
                        direction=close_dir,
                        amount=leg.get("amount", 1),
                        order_type="market"
                    )
                    results[leg["instrument"]] = {"status": "ok", "order": r}
                except Exception as e:
                    results[leg["instrument"]] = {"status": "error", "error": str(e)}
            hm = pm._hedge_map.get(signal_id, {})
            hedge_inst = hm.get("instrument", "")
            hedge_amt = hm.get("amount", 0)
            hedge_dir = hm.get("direction", "")
            if hedge_inst and hedge_amt and hedge_amt > 0:
                close_dir = "sell" if hedge_dir == "buy" else "buy"
                try:
                    r = await trader.place_order(
                        instrument=hedge_inst, direction=close_dir,
                        amount=hedge_amt, order_type="market"
                    )
                    results[hedge_inst] = {"status": "ok", "order": r}
                except Exception as e:
                    results[hedge_inst] = {"status": "error", "error": str(e)}
            if pm:
                pm.close_position(signal_id)
        else:
            # 没有 signal_id → 按 instrument 直接平仓
            inst = signal_id
            currency = inst.split("-")[0] if "-" in inst else "BTC"
            try:
                # 从交易所查持仓量
                pos_result = await trader._send("private/get_position", {
                    "instrument_name": inst
                })
                pos_data = pos_result if isinstance(pos_result, dict) else {}
                size = abs(pos_data.get("size", 0))
                if size > 0:
                    close_dir = "sell" if pos_data.get("size", 0) > 0 else "buy"
                    r = await trader.place_order(
                        instrument=inst, direction=close_dir,
                        amount=size, order_type="market"
                    )
                    results[inst] = {"status": "ok", "order": r}
                else:
                    results[inst] = {"status": "skipped", "reason": "无持仓"}
            except Exception as e:
                results[inst] = {"status": "error", "error": str(e)}

    # 同步停止该币种格致对冲
    gl = state.get("gl_hedge")
    if gl:
        try:
            await gl.stop_delta_hedge(currency=currency)
        except Exception:
            pass

    return {"status": "ok", "results": results}


@app.post("/api/gl-hedge/login")
async def api_gl_hedge_login(cred: GlHedgeCredentials):
    try:
        from execution.greeks_live_hedge import GreeksLiveHedge
        gl = GreeksLiveHedge(cred.email, cred.pwd)
        ok = await gl.login()
        if ok:
            await gl.get_account_id()
            state["gl_hedge"] = gl
            # 保存到 .env
            env = _load_env_simple(ENV_PATH)
            env["GELI_EMAIL"] = cred.email
            env["GELI_PASSWORD"] = cred.pwd
            _save_env_simple(ENV_PATH, env)
            logger.info("格致凭证已保存到 .env")
            return {"status": "ok", "account_id": gl.account_id}
        return {"status": "error", "message": "Login failed"}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@app.post("/api/gl-hedge/start")
async def api_gl_hedge_start(request: Request, currency: str = "BTC"):
    gl = state.get("gl_hedge")
    if not gl:
        return {"status": "error", "message": "Not logged in"}
    body = {}
    try:
        body = await request.json()
    except Exception:
        body = {}
    result = await gl.start_delta_hedge(
        currency=currency,
        coin_target_delta=float(body.get("coin_target_delta", 0)),
        max_positive=float(body.get("max_positive", 0.5)),
        max_negative=float(body.get("max_negative", 0.5)),
        order_type=body.get("order_type", "taker"),
    )
    if result.get("status") == "ok":
        pm = state.get("position_manager")
        if pm:
            for p in pm.positions.values():
                if p.currency == currency and p.status == "open":
                    p.hedge_active = True
    return result


@app.post("/api/gl-hedge/stop")
async def api_gl_hedge_stop(currency: str = "BTC"):
    gl = state.get("gl_hedge")
    if not gl:
        return {"status": "error", "message": "Not logged in"}
    return await gl.stop_delta_hedge(currency=currency)


@app.get("/api/gl-hedge/status")
async def api_gl_hedge_status(currency: str = "BTC"):
    gl = state.get("gl_hedge")
    if not gl:
        return {"status": "error", "message": "Not logged in"}
    return await gl.get_hedge_status(currency=currency)


@app.post("/api/params")
async def api_set_params(request: Request):
    """前端动态设置扫描参数（覆盖 config 默认，主循环下一轮生效）"""
    try:
        body = await request.json()
    except Exception:
        body = {}
    allowed = {"min_dte", "delta_min", "delta_max", "z_threshold", "min_oi"}
    params = {k: body[k] for k in allowed if k in body}
    state["runtime_params"].update(params)
    logger.info(f"运行时参数更新: {params}")
    return {"status": "ok", "runtime_params": state["runtime_params"]}


@app.get("/api/params")
async def api_get_params():
    """获取当前运行时参数（前端初始化滑块用）"""
    return {"runtime_params": state.get("runtime_params", {})}
