"""
期权天眼 — 格致 Trial Forge API 对冲客户端
通过格致 DynamicDeltaStrategy Pro 模式做 Delta 中性对冲
"""
import asyncio
import json
import logging
import time
from typing import Optional

import aiohttp

logger = logging.getLogger(__name__)

AUTH_URL = "https://test-auth.greeks.live/api/v1/auth/sign_in"
ACCOUNT_URL = "https://test-auth.greeks.live/api/v1/account/list"
TOOLS_URL = "https://test-tools.greeks.live/api"


class GreeksLiveHedge:
    """格致对冲管理"""

    def __init__(self, email: str, pwd: str, account_id: str = None):
        self.email = email
        self.pwd = pwd
        self.account_id = account_id or ""
        self.token: Optional[str] = None
        self._session: Optional[aiohttp.ClientSession] = None

    async def _ensure_session(self):
        if not self._session:
            self._session = aiohttp.ClientSession()

    async def login(self) -> bool:
        """登录拿 token"""
        await self._ensure_session()
        try:
            async with self._session.post(
                AUTH_URL,
                json={"email": self.email, "pwd": self.pwd}
            ) as resp:
                data = await resp.json()
                if data.get("code") == 0:
                    self.token = data["data"]["token"]
                    logger.info("格致登录成功")
                    return True
                else:
                    logger.error(f"格致登录失败: {data.get('message')}")
                    return False
        except Exception as e:
            logger.error(f"格致登录异常: {e}")
            return False

    async def get_account_id(self) -> Optional[str]:
        """获取交易账户 ID"""
        if not self.token:
            if not await self.login():
                return None
        await self._ensure_session()
        try:
            async with self._session.post(
                ACCOUNT_URL,
                json={"exchange": "deribit", "source": "adv_tools"},
                headers={"Authorization": self.token}
            ) as resp:
                data = await resp.json()
                accounts = data.get("data", {}).get("accounts", [])
                if accounts:
                    acc = accounts[0]
                    self.account_id = acc["id"]
                    logger.info(f"格致账户: {acc.get('account_name')} ID={self.account_id}")
                    return self.account_id
        except Exception as e:
            logger.error(f"格致账户查询失败: {e}")
        return None

    async def start_delta_hedge(
        self,
        currency: str = "BTC",
        coin_target_delta: float = 0,
        max_positive: float = 0.5,
        max_negative: float = 0.5,
        order_type: str = "taker",
        maker_layers: Optional[list] = None,
    ) -> dict:
        """
        启动 DynamicDeltaStrategy Pro 模式对冲

        参数:
            currency: BTC / ETH
            coin_target_delta: 币本位目标 Delta（0 = Delta 中性）
            max_positive/max_negative: 允许偏离范围
            order_type: maker / taker
            maker_layers: 两层 maker 模式参数（可选）
        """
        if not self.account_id:
            if not await self.get_account_id():
                return {"status": "error", "message": "No account ID"}

        if not self.token:
            return {"status": "error", "message": "Not logged in"}

        perpetual = f"{currency}-PERPETUAL"
        params = {
            "version": "pro",
            "coinDeltaMode": True,
            "coinTargetDelta": coin_target_delta,
            "usdTargetDelta": 0,
            "maxPositiveCoinDelta": max_positive,
            "maxNegativeCoinDelta": max_negative,
            "maxPositiveUsdDelta": 1000,
            "maxNegativeUsdDelta": 1000,
            "positiveHedgeRatio": 100,
            "negativeHedgeRatio": 100,
            "longFuture": perpetual,
            "shortFuture": perpetual,
            "orderType": order_type,
            "makerEachOrderSize": 0.2,
            "takerEachOrderSize": 0.05,
            "pos_mode": "all",
            "sel_pos_list": [],
            "ignore_pos_list": [],
            "hedge_spot": False,
        }

        if maker_layers:
            params["orderType"] = "maker"
            params["makerLayers"] = maker_layers
            params["hardDeltaBandEquityRatio"] = 0.05
            params["hardDeltaTakerRatio"] = 0.5
            params["hardDeltaBandDelaySec"] = 60

        # 设参+启动
        try:
            async with self._session.post(
                f"{TOOLS_URL}/strategy/setParamsAndStart",
                json={
                    "account_id": self.account_id,
                    "currency": currency,
                    "strategy_type": "DynamicDeltaStrategy",
                    "params": params,
                },
                headers={"Authorization": self.token}
            ) as resp:
                result = await resp.json()
                if result.get("code") == 200:
                    logger.info(f"格致对冲已启动: {currency} target={coin_target_delta}")
                    return {"status": "ok", "data": result.get("data")}
                else:
                    logger.error(f"格致对冲启动失败: {result}")
                    return {"status": "error", "message": str(result)}
        except Exception as e:
            logger.error(f"格致对冲请求异常: {e}")
            return {"status": "error", "message": str(e)}

    async def stop_delta_hedge(self, currency: str) -> dict:
        """停止对冲"""
        if not self.account_id or not self.token:
            return {"status": "error", "message": "Not ready"}
        try:
            async with self._session.post(
                f"{TOOLS_URL}/strategy/stop",
                json={
                    "account_id": self.account_id,
                    "currency": currency,
                    "strategy_type": "DynamicDeltaStrategy",
                },
                headers={"Authorization": self.token}
            ) as resp:
                result = await resp.json()
                return {"status": "ok" if result.get("code") == 200 else "error"}
        except Exception as e:
            return {"status": "error", "message": str(e)}

    async def get_hedge_status(self, currency: str) -> dict:
        """查询对冲状态"""
        if not self.account_id or not self.token:
            return {"status": "error", "message": "Not ready"}
        try:
            async with self._session.post(
                f"{TOOLS_URL}/strategy/get",
                json={
                    "account_id": self.account_id,
                    "currency": currency,
                    "strategy_type": "DynamicDeltaStrategy",
                },
                headers={"Authorization": self.token}
            ) as resp:
                result = await resp.json()
                if result.get("code") == 200:
                    data = result.get("data", {})
                    return {
                        "status": "ok",
                        "is_run": data.get("is_run", False),
                        "params": data.get("params", {}),
                        "start_time": data.get("start_time"),
                    }
                return {"status": "error", "message": str(result)}
        except Exception as e:
            return {"status": "error", "message": str(e)}

    async def close(self):
        if self._session:
            await self._session.close()
