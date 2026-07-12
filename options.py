# options.py
"""中证1000股指期权(MO) IV 计算 + 卖 call 增厚率分析。"""
from __future__ import annotations
import math


def _norm_cdf(x: float) -> float:
    """标准正态分布 CDF（Abramowitz-Stegun 近似）。"""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2)))


def black_scholes_call(
    S: float, K: float, T: float, r: float = 0.02, q: float = 0.015, sigma: float = 0.25
) -> float:
    """带股息率的 BS call 定价。T 为年化时间。"""
    if T <= 0 or sigma <= 0:
        return max(S * math.exp(-q * T) - K * math.exp(-r * T), 0.0)
    sqrtT = math.sqrt(T)
    d1 = (math.log(S / K) + (r - q + sigma ** 2 / 2) * T) / (sigma * sqrtT)
    d2 = d1 - sigma * sqrtT
    return S * math.exp(-q * T) * _norm_cdf(d1) - K * math.exp(-r * T) * _norm_cdf(d2)


def implied_vol(
    market_price: float, S: float, K: float, T: float,
    r: float = 0.02, q: float = 0.015,
) -> float:
    """二分法反解 IV。市场价不合理时返回 0。"""
    intrinsic = max(S * math.exp(-q * T) - K * math.exp(-r * T), 0.0)
    if market_price <= intrinsic:
        return 0.0
    lo, hi = 0.005, 3.0
    for _ in range(100):
        mid = (lo + hi) / 2
        price = black_scholes_call(S, K, T, r, q, mid)
        if price < market_price:
            lo = mid
        else:
            hi = mid
        if hi - lo < 0.0001:
            break
    return (lo + hi) / 2


def prob_above_strike(S: float, K: float, T: float, sigma: float,
                      r: float = 0.02, q: float = 0.015) -> float:
    """到期日 S_T > K 的风险中性概率（即 call 被行权概率）。"""
    if T <= 0 or sigma <= 0:
        return 1.0 if S > K else 0.0
    sqrtT = math.sqrt(T)
    d2 = (math.log(S / K) + (r - q - sigma ** 2 / 2) * T) / (sigma * sqrtT)
    return _norm_cdf(d2)


def annualized_enhancement(premium: float, spot: float, days: float) -> float:
    """年化增厚率(%) = (premium / spot) × (365 / days) × 100。"""
    if days <= 0 or spot <= 0:
        return 0.0
    return premium / spot * 365.0 / days * 100.0


def implied_forward(call_price: float, put_price: float, K: float, T: float,
                    r: float = 0.02) -> float:
    """Put-Call Parity 反推远期: F = K + (C - P) × e^(rT)。"""
    if T <= 0:
        return K + call_price - put_price
    return K + (call_price - put_price) * math.exp(r * T)
