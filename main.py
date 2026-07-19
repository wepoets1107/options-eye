"""
期权天眼 — 主入口

启动 Deribit WebSocket 数据流、SABR 校准引擎、信号引擎、推送通知和 Web 工作台
"""
import asyncio
import logging
import logging.handlers
import os
import time
from pathlib import Path

import yaml
import uvicorn

from data.deribit_ws import DeribitWS
from data.chain_cache import build_chain_snapshot
from version import __version__ as APP_VERSION
from sabr.calibrator import calibrate_all
from strategy.detector import detect_deviations, generate_signals
from notification.notifier import NotificationManager
from notification.bnb_client import RollingMarket, BinanceFuturesClient
from notification.expiry_scorer import ExpiryScorer

logger = logging.getLogger(__name__)


def setup_logging(config: dict):
    """配置日志"""
    log_cfg = config.get("logging", {})
    level = getattr(logging, log_cfg.get("level", "INFO").upper(), logging.INFO)
    log_file = log_cfg.get("file", "logs/options-eye.log")
    max_bytes = log_cfg.get("max_size_mb", 10) * 1024 * 1024
    backup = log_cfg.get("backup_count", 3)

    root = logging.getLogger()
    root.setLevel(level)

    # 控制台
    ch = logging.StreamHandler()
    ch.setLevel(level)
    ch.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
    root.addHandler(ch)

    # 文件（轮转）
    os.makedirs(os.path.dirname(log_file) or ".", exist_ok=True)
    fh = logging.handlers.RotatingFileHandler(
        log_file, maxBytes=max_bytes, backupCount=backup
    )
    fh.setLevel(level)
    fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
    root.addHandler(fh)


def load_config() -> dict:
    """加载配置：优先 config.yaml，回退 config.example.yaml（公共仓库无本地 config 时用）"""
    base = Path(__file__).parent
    for name in ("config.yaml", "config.example.yaml"):
        p = base / name
        if p.exists():
            with open(p, "r", encoding="utf-8") as f:
                return yaml.safe_load(f)
    return {}


def inject_real_greeks(ws: DeribitWS, slices: list):
    """从 ticker 推送缓存注入真实 Greeks（实时 pa delta 为全链路基准，零网络请求）"""
    for s in slices:
        for c in (s.calls + s.puts):
            g = ws.get_cached_greeks(c.instrument)
            if g:
                c.delta = g["delta"]
                c.gamma = g["gamma"]
                c.vega = g["vega"]
                c.theta = g["theta"]
                if g.get("mark_price"):
                    c.mark_price = g["mark_price"]


async def main_loop(
    ws: DeribitWS,
    config: dict,
    web_state: dict
):
    """
    主循环:
    1. 等待 WebSocket 缓存数据
    2. 构建期权链快照
    3. SABR 校准
    4. 偏差检测
    5. 信号生成
    """
    filters = config.get("filters", {})
    sabr_cfg = config.get("sabr", {})
    scan_cfg = config.get("scan", {})

    sabr_interval = sabr_cfg.get("calibrate_interval_sec", 30)
    last_calibrate = 0
    instr_refresh_sec = scan_cfg.get("instrument_refresh_sec", 300)
    last_instr_refresh = 0

    while True:
        try:
            now = time.time()

            # 0. ticker 推送缓存自检（全推送数据源，运行期不轮询任何行情接口）
            if not ws.ticker_cache:
                logger.info("ticker 缓存尚未就绪，等待推送...")
                await asyncio.sleep(2)
                continue

            # 0.5 周期性刷新合约列表并重建 ticker 订阅（捕捉新上市到期/合约）
            if now - last_instr_refresh >= instr_refresh_sec:
                try:
                    insts = []
                    for cur in ("BTC", "ETH"):
                        r = await ws.get_instruments(cur)
                        insts += [i["instrument_name"] for i in r]
                    ws.instruments = insts
                    await ws.manage_ticker_subscriptions(insts)
                    logger.info(f"合约列表刷新: {len(insts)} 个")
                except Exception as e:
                    logger.warning(f"合约列表刷新失败: {e}")
                last_instr_refresh = now

            # 1. 构建期权链快照（直接读 ticker 推送缓存）
            #    运行时参数（前端滑块）优先，覆盖 config 默认
            rp = web_state.get("runtime_params", {})
            rp_dte = rp.get("min_dte", filters.get("min_dte", 7))
            rp_oi = rp.get("min_oi", filters.get("min_oi", 10))
            max_age = sabr_cfg.get("max_data_age_minutes", 5) * 60
            raw = ws.get_all_ticker(max_age)

            # BTC（指数价仅读缓存，不触发网络请求；缓存为空时用上次价格兜底）
            btc_price = ws.index_price.get("btc_usdc") or web_state.get("_prev_btc_price", 0)
            btc_slices = build_chain_snapshot(
                raw, btc_price or 0, "BTC",
                min_dte=rp_dte,
                max_dte=filters.get("max_dte", 365),
                min_oi=rp_oi
            )

            # ETH
            eth_price = ws.index_price.get("eth_usdc") or web_state.get("_prev_eth_price", 0)
            eth_slices = build_chain_snapshot(
                raw, eth_price or 0, "ETH",
                min_dte=rp_dte,
                max_dte=filters.get("max_dte", 365),
                min_oi=rp_oi
            )

            all_slices = btc_slices + eth_slices

            # 2.5 注入真实 Greeks（pa delta 为全链路基准，读推送缓存）
            inject_real_greeks(ws, all_slices)

            # 3. SABR 校准（每 sabr_interval 秒一次）
            sabr_params = {}
            if now - last_calibrate >= sabr_interval:
                sabr_params = calibrate_all(
                    all_slices,
                    beta=sabr_cfg.get("beta", 0.7),
                    alpha_min=sabr_cfg.get("alpha_min", 0.01),
                    alpha_max=sabr_cfg.get("alpha_max", 30.0),
                    rho_min=sabr_cfg.get("rho_min", -0.8),
                    rho_max=sabr_cfg.get("rho_max", 0.5),
                    nu_min=sabr_cfg.get("nu_min", 0.05),
                    nu_max=sabr_cfg.get("nu_max", 2.0),
                    min_strikes=sabr_cfg.get("min_strikes_per_expiry", 5)
                )
                last_calibrate = now
                logger.info(f"校准完成: {len(sabr_params)} 个到期日")
            else:
                sabr_params = web_state.get("latest_sabr_params", {})

            # 4. 偏差检测（运行时参数 rp 已在 #1 读取）
            z_threshold = rp.get("z_threshold", filters.get("z_score_threshold", 2.0))
            delta_min = rp.get("delta_min", filters.get("delta_min", 0.05))
            delta_max = rp.get("delta_max", filters.get("delta_max", 0.25))
            min_oi = rp_oi

            deviations = detect_deviations(
                all_slices, sabr_params,
                z_threshold=z_threshold,
                delta_min=delta_min,
                delta_max=delta_max,
                min_oi=min_oi
            )

            # 5. 信号生成（传 delta 参数供 _classify 动态分档）
            signals = generate_signals(
                deviations, all_slices, sabr_params,
                z_threshold=z_threshold, delta_min=delta_min, delta_max=delta_max
            )

            # 6. 更新 Greeks 已由 user.positions 推送实时更新，不再手动 sync

            # 7. 更新全局状态（Web 工作台读取）
            web_state["latest_slices"] = all_slices
            web_state["latest_sabr_params"] = sabr_params
            web_state["latest_deviations"] = deviations
            web_state["latest_signals"] = [s.__dict__ for s in signals]
            web_state["last_update"] = int(now)

            if signals:
                for s in signals:
                    logger.info(f"信号: {s.description}")

            # 保存本次价格供下次兜底
            if btc_price:
                web_state["_prev_btc_price"] = btc_price
            if eth_price:
                web_state["_prev_eth_price"] = eth_price

            # 8. 推送通知（异步，不阻塞主循环）
            nf = web_state.get("notifier")
            if nf:
                signal_dicts = [s.__dict__ for s in signals]
                asyncio.create_task(nf.push_sabr_signals(signal_dicts))

        except Exception as e:
            logger.error(f"主循环异常: {e}", exc_info=True)

        await asyncio.sleep(max(1, scan_cfg.get("signal_interval_sec", 30)))


async def expiry_eval_loop(web_state: dict):
    """末日期权评分循环"""
    while True:
        try:
            scorer = web_state.get("expiry_scorer")
            ws = web_state.get("deribit_ws")
            notif = web_state.get("notifier")
            if not scorer or not ws or not notif:
                await asyncio.sleep(10)
                continue

            # 从 deribit_ws 取指数价格
            idx = ws.index_price.get("btc_usd", 0) if hasattr(ws, "index_price") else 0

            # 拉 Deribit 期权 book_summary
            opts = []
            try:
                raw = await ws.get_book_summary_by_currency("BTC")
                opts = raw if isinstance(raw, list) else []
            except Exception:
                pass

            if idx and opts:
                scorer.update_deribit(idx, opts)
                sig = scorer.evaluate()
                if sig:
                    logger.info(f"末日期权信号: {sig['signal']} conf={sig['confidence']}")
                    asyncio.create_task(notif.push_expiry_signal(sig))
        except Exception as e:
            logger.warning(f"末日期权评分异常: {e}")
        await asyncio.sleep(15)  # 每15秒评估一次


async def main():
    config = load_config()
    setup_logging(config)

    logger.info("=" * 50)
    logger.info("期权天眼 启动")
    logger.info("=" * 50)

    deribit_cfg = config.get("deribit", {})
    public_ws = deribit_cfg.get("public_ws", "wss://www.deribit.com/ws/api/v2")
    trade_ws = deribit_cfg.get("trade_ws", "wss://test.deribit.com/ws/api/v2")
    ticker_interval = deribit_cfg.get("ticker_interval", "100ms")

    logger.info(f"数据源: {public_ws}")
    logger.info(f"交易端: {trade_ws}")
    logger.info(f"ticker 推送间隔: {ticker_interval}")

    # 初始化公共数据 WebSocket（走主网，全推送）
    ws = DeribitWS(public_ws, ticker_interval=ticker_interval)
    await ws.connect()  # 内部完成：合约列表 -> ticker 多连接订阅 + 指数订阅 + 冷启动填充
    logger.info("初始数据订阅完成")

    # 初始化 Web 状态
    from web.app import state as web_state

    # 持仓管理器
    from execution.position_manager import PositionManager
    pm = PositionManager()
    web_state["position_manager"] = pm

    web_state["config"] = config
    web_state["deribit_ws"] = ws
    web_state["is_running"] = True
    web_state["version"] = APP_VERSION

    # 初始化推送通知
    notifier = NotificationManager(config)
    web_state["notifier"] = notifier
    logger.info(f"推送通知模块已初始化 (enabled={notifier.enabled})")

    # 初始化末日期权评分
    market = RollingMarket()
    expiry_scorer = ExpiryScorer(market)
    web_state["expiry_scorer"] = expiry_scorer
    bnb_client = BinanceFuturesClient(market)
    web_state["bnb_client"] = bnb_client
    asyncio.create_task(bnb_client.run())
    logger.info("末日期权评分模块已初始化 (等待 Binance WS 数据积累)")

    # 启动主循环（后台）
    asyncio.create_task(main_loop(ws, config, web_state))

    # 启动末日期权评分（后台）
    asyncio.create_task(expiry_eval_loop(web_state))

    # 启动 Web 服务器
    web_cfg = config.get("web", {})
    host = web_cfg.get("host", "127.0.0.1")
    port = web_cfg.get("port", 5051)

    logger.info(f"Web 工作台: http://{host}:{port}")
    config_uvicorn = uvicorn.Config(
        "web.app:app",
        host=host,
        port=port,
        log_level="info",
        reload=False
    )
    server = uvicorn.Server(config_uvicorn)
    await server.serve()


if __name__ == "__main__":
    asyncio.run(main())
