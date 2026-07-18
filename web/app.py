"""
期权天眼 — FastAPI Web 工作台后端
"""
import asyncio
import json
import logging
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
}


from pydantic import BaseModel


class ExecuteRequest(BaseModel):
    signal_id: str


class TradeCredentials(BaseModel):
    client_id: str
    client_secret: str


class GlHedgeCredentials(BaseModel):
    email: str
    pwd: str


@app.on_event("startup")
async def startup():
    # 挂载静态文件
    static_dir = Path(__file__).parent / "static"
    static_dir.mkdir(exist_ok=True)
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")


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


@app.post("/api/credentials")
async def api_credentials(cred: TradeCredentials):
    """设置 Deribit 交易凭证"""
    try:
        from execution.deribit_trader import DeribitTrader
        # 交易凭证默认连测试网（配置文件 testnet=false 只控制数据源）
        trader = DeribitTrader(cred.client_id, cred.client_secret, testnet=True)
        await trader.connect()
        state["trader"] = trader
        state["client_id"] = cred.client_id
        return {"status": "ok", "message": "Trader connected"}
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
        results = await trader.execute_signal(signal)
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
                legs=signal["legs"], hedge_instrument=signal.get("hedge_instrument",""),
                hedge_direction=signal.get("hedge_direction",""),
                hedge_amount=signal.get("hedge_amount",0),
                expected_premium=signal.get("expected_premium",0),
                estimated_delta=signal.get("estimated_delta",0),
                deviations=[], created_at=int(time.time()))
            pm.add_position(sig)
            # 回写实际成交的对冲量（hedge_ok 用成交 amount，hedge_skipped/失败则置 0），
            # 保证后续平仓时对冲腿 amount 准确
            hedge_inst = signal.get("hedge_instrument", "")
            pos = pm.positions.get(signal["id"])
            if pos and hedge_inst:
                hr = results.get(hedge_inst, {})
                if isinstance(hr, dict) and hr.get("status") == "hedge_ok":
                    pos.hedge_amount = float(hr.get("amount", 0))
                else:
                    pos.hedge_amount = 0.0   # skipped / error / 未对冲
            # 执行后立即刷新 Greeks，避免前 30 秒显示 0
            try:
                pm.update_greeks(state["latest_sabr_params"], state["latest_slices"])
            except Exception as e:
                logger.warning(f"Immediate greeks update failed: {e}")
        return {"status": "ok", "results": results}
    except Exception as e:
        logger.error(f"Execute failed: {e}")
        return {"status": "error", "message": str(e)}


@app.get("/api/positions")
async def api_positions():
    pm = state.get("position_manager")
    if not pm:
        return {"positions": [], "summary": {}}
    return {"positions": pm.get_all_positions(), "summary": pm.get_net_position()}


@app.post("/api/position/{signal_id}/close")
async def api_close_position(signal_id: str):
    """平仓：反向平掉期权腿 + 对冲腿，并停止格致对冲"""
    pm = state.get("position_manager")
    pos = pm.positions.get(signal_id) if pm else None
    trader = state.get("trader")
    results = {}

    if pos and trader:
        # 反向平掉每个期权腿
        for leg in pos.legs:
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
        # 平掉对冲腿（永续）
        if pos.hedge_instrument and pos.hedge_amount and pos.hedge_amount > 0:
            hedge_close_dir = "sell" if pos.hedge_direction == "buy" else "buy"
            try:
                r = await trader.place_order(
                    instrument=pos.hedge_instrument,
                    direction=hedge_close_dir,
                    amount=pos.hedge_amount,
                    order_type="market"
                )
                results[pos.hedge_instrument] = {"status": "ok", "order": r}
            except Exception as e:
                results[pos.hedge_instrument] = {"status": "error", "error": str(e)}

    if pm:
        pm.close_position(signal_id)

    # 同步停止该币种格致对冲
    gl = state.get("gl_hedge")
    if gl and pos:
        try:
            await gl.stop_delta_hedge(currency=pos.currency)
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
