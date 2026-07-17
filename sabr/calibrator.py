"""
期权天眼 — SABR 校准引擎

基于 scipy.optimize.least_squares 校准 (α, ρ, ν)，β 固定为 0.7

SABR 隐含波动率近似公式 (Hagan 2002)：
σ(K, F) = α / (F^(1-β) * (1 + (1-β)²/24 * (ln(F/K))² + (1-β)^4/1920 * (ln(F/K))^4)) * (z/x(z))

其中 z = ν/α * F^(1-β) * ln(F/K)
     x(z) = ln((√(1 - 2ρz + z²) + z - ρ) / (1 - ρ))
"""
import logging
import time
import numpy as np
from scipy.optimize import least_squares
from typing import Optional

from data.models import SabrParams, ExpirySlice, OptionContract

logger = logging.getLogger(__name__)


def sabr_iv(
    f: float,       # 远期价格
    k: float,       # 行权价
    t: float,       # 剩余时间（年）
    alpha: float,
    beta: float,
    rho: float,
    nu: float
) -> float:
    """
    Hagan 2002 SABR 隐含波动率公式
    返回小数形式的 IV
    """
    if abs(f - k) < 1e-8:
        # ATM 情形
        term1 = alpha / (f ** (1 - beta))
        term2 = 1 + ((2 - 3 * rho ** 2) / 24) * nu ** 2 * t
        return term1 * term2

    log_fk = np.log(f / k)
    f1b = f ** (1 - beta)

    z = (nu / alpha) * f1b * log_fk

    # x(z) 处理边界
    if abs(z) < 1e-8:
        xz = 1.0
    else:
        sqrt_term = np.sqrt(1 - 2 * rho * z + z ** 2)
        xz = np.log((sqrt_term + z - rho) / (1 - rho))

    num1 = alpha
    den1 = f1b * (1 + (1 - beta) ** 2 / 24 * log_fk ** 2
                  + (1 - beta) ** 4 / 1920 * log_fk ** 4)

    num2 = z
    den2 = xz

    term1 = (num1 / den1) * (num2 / den2)

    term2 = 1 + (
        (1 - beta) ** 2 / 24 * alpha ** 2 / (f1b ** 2)
        + 0.25 * rho * beta * nu * alpha / f1b
        + (2 - 3 * rho ** 2) / 24 * nu ** 2
    ) * t

    return term1 * term2


def _calc_bartlett_delta(
    f: float, k: float, t: float,
    alpha: float, beta: float, rho: float, nu: float,
    bs_delta: float, bs_vega: float
) -> float:
    """计算 Bartlett Delta（SABR 调整后的 Delta）"""
    eps = 1e-8
    iv = sabr_iv(f, k, t, alpha, beta, rho, nu)
    iv_up = sabr_iv(f + eps, k, t, alpha, beta, rho, nu)
    div_df = (iv_up - iv) / eps if iv_up > 0 else 0
    return bs_delta + bs_vega * div_df


def _residuals(params, f, strikes, market_ivs, t, beta):
    """least_squares 损失函数"""
    alpha, rho, nu = params
    residuals = []
    for k, miv in zip(strikes, market_ivs):
        if miv <= 0:
            continue
        try:
            model_iv = sabr_iv(f, k, t, alpha, beta, rho, nu)
            if np.isfinite(model_iv) and model_iv > 0:
                residuals.append(model_iv - miv)
        except:
            pass
    return residuals


def _atm_alpha(atm_iv: float, f: float, beta: float) -> float:
    """从 ATM IV 反推 alpha 初值: σ_ATM ≈ α / F^(1-β)"""
    if f <= 0:
        return atm_iv
    return max(atm_iv * (f ** (1 - beta)), 0.01)


def calibrate_expiry(
    slice_: ExpirySlice,
    beta: float = 0.7,
    alpha_min: float = 0.01,
    alpha_max: float = 30.0,
    rho_min: float = -0.8,
    rho_max: float = 0.5,
    nu_min: float = 0.05,
    nu_max: float = 2.0,
    min_strikes: int = 5
) -> Optional[SabrParams]:
    """
    对单个到期日进行 SABR 校准

    合并 call 和 put 的 IV 数据一起拟合
    """
    f = slice_.forward
    if not f or f <= 0:
        return None

    t = slice_.dte / 365.0
    if t <= 0:
        return None

    # 收集可用于校准的合约数据
    strikes = []
    ivs = []
    kinds = []

    for c in slice_.calls:
        if c.mark_iv > 0 and c.strike > 0:
            strikes.append(c.strike)
            ivs.append(c.mark_iv)
            kinds.append("call")

    for p in slice_.puts:
        if p.mark_iv > 0 and p.strike > 0:
            strikes.append(p.strike)
            ivs.append(p.mark_iv)
            kinds.append("put")

    if len(strikes) < min_strikes:
        logger.debug(f"Expiry {slice_.dte}d: 只有 {len(strikes)} 个数据点，跳过校准")
        return None

    strikes = np.array(strikes, dtype=float)
    ivs = np.array(ivs, dtype=float)

    # 初始值：从 ATM IV 反推 alpha
    alpha0 = _atm_alpha(slice_.atm_iv if slice_.atm_iv > 0 else 0.5, f, beta)
    alpha0 = np.clip(alpha0, alpha_min, alpha_max)
    rho0 = -0.2
    nu0 = 0.5

    # 边界
    bounds = (
        [alpha_min, rho_min, nu_min],
        [alpha_max, rho_max, nu_max]
    )

    try:
        result = least_squares(
            _residuals,
            [alpha0, rho0, nu0],
            args=(f, strikes, ivs, t, beta),
            bounds=bounds,
            method="trf",
            max_nfev=200,
            ftol=1e-8,
            xtol=1e-8
        )

        alpha, rho, nu = result.x
        rmse = np.sqrt(np.mean(result.fun ** 2)) if len(result.fun) > 0 else 999

        # 检查是否收敛
        converged = result.success and rmse < 0.5

        if converged:
            logger.info(
                f"SABR {slice_.dte}d: α={alpha:.4f} ρ={rho:.4f} ν={nu:.4f} "
                f"RMSE={rmse:.4f} 点={len(strikes)}"
            )

        return SabrParams(
            expiration=slice_.expiration,
            dte=slice_.dte,
            currency=slice_.currency,
            alpha=float(alpha),
            beta=beta,
            rho=float(rho),
            nu=float(nu),
            rmse=float(rmse),
            calibrated_at=int(time.time()),
            converged=converged
        )

    except Exception as e:
        logger.warning(f"Expiry {slice_.dte}d 校准失败: {e}")
        return None


def calibrate_all(
    slices: list[ExpirySlice],
    beta: float = 0.7,
    **kwargs
) -> dict[str, SabrParams]:
    """校准所有到期日，返回 key=f"{currency}_{expiration}" 的字典"""
    results = {}
    for s in slices:
        params = calibrate_expiry(s, beta=beta, **kwargs)
        if params and params.converged:
            key = f"{s.currency}_{s.expiration}"
            results[key] = params
    return results


def expected_iv(
    f: float, k: float, t: float,
    sabr: SabrParams
) -> float:
    """用已校准的 SABR 参数计算某行权价的期望 IV"""
    return sabr_iv(f, k, t, sabr.alpha, sabr.beta, sabr.rho, sabr.nu)


def compute_bartlett_delta(
    contract: OptionContract,
    slice_: ExpirySlice,
    sabr: SabrParams
) -> float:
    """计算 Bartlett Delta"""
    t = slice_.dte / 365.0
    bs_delta = contract.delta
    bs_vega = contract.vega
    return _calc_bartlett_delta(
        slice_.forward, contract.strike, t,
        sabr.alpha, sabr.beta, sabr.rho, sabr.nu,
        bs_delta, bs_vega
    )
