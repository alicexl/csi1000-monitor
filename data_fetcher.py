# data_fetcher.py
from __future__ import annotations
import calendar
import math
import time
from datetime import date, datetime
from typing import Any, Callable

import akshare as ak


# ─── 基差/贴水/合约分类（原 basis.py）─────────────────────────
QUARTER_MONTHS = [3, 6, 9, 12]


def compute_basis(futures_close: float, spot_close: float) -> float:
    """基差 = 期货收盘 - 现货收盘。负值=贴水，正值=升水。"""
    return futures_close - spot_close


def compute_annualized_discount(
    futures_close: float, spot_close: float, days_to_expire: int
) -> float:
    """年化贴水率 %。正值=贴水收益，负值=升水亏损。days=0 返回 0 防除零。"""
    if days_to_expire <= 0 or spot_close == 0:
        return 0.0
    discount_rate = (spot_close - futures_close) / spot_close * 100
    return discount_rate * 365 / days_to_expire


def third_friday(year: int, month: int) -> date:
    """某年月的第三个周五（中金所股指期货交割日）。"""
    cal = calendar.Calendar()
    fridays = [
        d for d in cal.itermonthdates(year, month)
        if d.month == month and d.weekday() == 4
    ]
    return fridays[2]


def days_to_expire(today: date, expire_date: date) -> int:
    """剩余天数（今日到交割日的日历天数）。"""
    return (expire_date - today).days


def classify_contract(
    symbol: str, today: date
) -> tuple[str | None, date | None]:
    """识别 IM 合约类型（当月/下月/当季/下季）+ 交割日。非 IM 合约返回 (None, None)。

    当月交割日（含）之后：CFFEX 日数据是盘后发布的，旧当月已交割 → 分类参考月份
    移到下月，旧下月自动成为新当月。旧当月合约（ctype=None）在 fetch 时被过滤，
    避免 DB 里残留"已交割合约 + 无意义基差"的行。
    """
    if not symbol.startswith("IM") or len(symbol) != 6:
        return None, None
    try:
        yy = int(symbol[2:4])
        mm = int(symbol[4:6])
    except ValueError:
        return None, None
    if not (1 <= mm <= 12):
        return None, None

    year = 2000 + yy
    expire = third_friday(year, mm)

    # 当月已交割（today >= 本月第三个周五）→ 分类参考月份移到下月
    this_month_expire = third_friday(today.year, today.month)
    if today >= this_month_expire:
        if today.month == 12:
            ref_y, ref_m = today.year + 1, 1
        else:
            ref_y, ref_m = today.year, today.month + 1
    else:
        ref_y, ref_m = today.year, today.month

    ref_yyyymm = ref_y * 12 + ref_m
    contract_yyyymm = year * 12 + mm
    if contract_yyyymm == ref_yyyymm:
        return "当月", expire
    if contract_yyyymm == ref_yyyymm + 1:
        return "下月", expire

    future_quarters = []
    y, m = ref_y, ref_m
    m += 1
    if m > 12:
        m = 1
        y += 1
    while len(future_quarters) < 4:
        if m in QUARTER_MONTHS:
            future_quarters.append((y, m))
        m += 1
        if m > 12:
            m = 1
            y += 1
    for i, label in [(0, "当季"), (1, "下季")]:
        if i < len(future_quarters):
            qy, qm = future_quarters[i]
            if qy == year and qm == mm:
                return label, expire
    return None, expire


# ─── 期权 BS 定价（原 options.py）─────────────────────────────
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
    """到期日 S_T > K 的风险中性概率（call 被行权概率）。"""
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


# ─── akshare 拉取 ────────────────────────────────────────────
def retry(fn: Callable, retries: int = 3, delays: tuple = (2, 4, 8)) -> Any:
    """指数退避重试。全部失败返回 None。"""
    last_err = None
    for i in range(retries):
        try:
            return fn()
        except Exception as e:
            last_err = e
            if i < retries - 1:
                time.sleep(delays[i])
    print(f"[WARN] 重试 {retries} 次仍失败: {last_err}", flush=True)
    return None


def build_valuation_row(row: dict) -> dict:
    """从 merged row（PE + PB 已 join）构造 daily_valuation 记录。"""
    return {
        "date": str(row["date"])[:10],
        "close": float(row["close"]),
        "pe_ttm": float(row.get("pe_ttm", 0)),
        "pb": float(row.get("pb", 0)),
        "fetched_at": datetime.now().isoformat(timespec="seconds"),
    }


def fetch_valuation() -> list[dict]:
    """拉 PE/PB 全历史 → 返回合并行列表（升序）。"""
    pe_raw = retry(lambda: ak.stock_index_pe_lg(symbol="中证1000"))
    pb_raw = retry(lambda: ak.stock_index_pb_lg(symbol="中证1000"))
    if pe_raw is None or pb_raw is None:
        return []

    pe_df = pe_raw.rename(columns={
        "日期": "date", "指数": "close",
        "滚动市盈率": "pe_ttm",
    })
    pb_df = pb_raw.rename(columns={
        "日期": "date", "指数": "close", "市净率": "pb",
    })

    merged = pe_df.merge(pb_df[["date", "pb"]], on="date", how="inner")
    merged = merged.sort_values("date").reset_index(drop=True)

    rows = []
    for _, r in merged.iterrows():
        try:
            rows.append(build_valuation_row(r.to_dict()))
        except (ValueError, TypeError):
            continue
    return rows


def fetch_daily_contracts(today: date, spot_close: float) -> list[dict]:
    """拉当天中金所 IM 合约 → 分类 + 算基差/贴水。"""
    today_str = today.strftime("%Y%m%d")
    df = retry(lambda: ak.get_futures_daily(
        start_date=today_str, end_date=today_str, market="CFFEX"))
    if df is None:
        return []

    rows = []
    for _, r in df.iterrows():
        symbol = str(r.get("symbol", ""))
        if not symbol.startswith("IM"):
            continue
        try:
            close = float(r["close"])
            ctype, expire = classify_contract(symbol, today)
            if ctype is None:
                continue
            d_val = str(r.get("date", today_str))
            # 兼容 20260710 / 2026-07-10 两种格式
            if len(d_val) == 8 and "-" not in d_val:
                d_val = f"{d_val[:4]}-{d_val[4:6]}-{d_val[6:8]}"
            days_left = days_to_expire(today, expire) if expire else 0
            rows.append({
                "date": d_val,
                "symbol": symbol,
                "name": f"中证1000 {symbol[-2:]}",
                "contract_type": ctype,
                "close": close,
                "settle": float(r.get("settle", close)),
                "volume": float(r.get("volume", 0)),
                "open_interest": float(r.get("open_interest", 0)),
                "expire_date": expire.isoformat() if expire else None,
                "days_to_expire": days_left,
                "basis": compute_basis(close, spot_close),
                "annualized_discount": compute_annualized_discount(close, spot_close, days_left),
                "fetched_at": datetime.now().isoformat(timespec="seconds"),
            })
        except (ValueError, TypeError, KeyError):
            continue
    return rows


def _pick_option_month(today: date, switch_days: int = 7) -> tuple[str, date]:
    """选卖 call 用的期权合约月份。当月剩余 < switch_days 时用下月。"""
    expire_cur = third_friday(today.year, today.month)
    if (expire_cur - today).days >= switch_days:
        mo_sym = f"MO{today.year % 100:02d}{today.month:02d}"
        return mo_sym, expire_cur
    yy, mm = today.year, today.month
    if mm == 12:
        yy, mm = yy + 1, 1
    else:
        mm += 1
    expire_next = third_friday(yy, mm)
    mo_sym = f"MO{yy % 100:02d}{mm:02d}"
    return mo_sym, expire_next


def fetch_otm_call(
    spot: float, today: date, otm_pct: float = 10.0, switch_days: int = 7
) -> dict | None:
    """拉当月/下月中证1000股指期权 → 选 OTM% 最近的 call → 算 IV/增厚率/行权概率。

    返回 None 表示拉取失败（周末/网络/接口异常），不阻断主流程。
    """
    mo_symbol, expire = _pick_option_month(today, switch_days)
    df = retry(lambda: ak.option_cffex_zz1000_spot_sina(symbol=mo_symbol), retries=2)
    if df is None or df.empty:
        return None

    target_strike = spot * (1 + otm_pct / 100.0)
    best = None
    best_diff = 1e9
    for _, r in df.iterrows():
        try:
            k = float(r.iloc[7])  # 行权价
            diff = abs(k - target_strike)
            if diff < best_diff:
                cs = r.iloc[3]  # call 卖价
                if str(cs) in ("-", "", "None"):
                    continue
                best_diff = diff
                best = {
                    "symbol": mo_symbol,
                    "strike": k,
                    "call_sell": float(cs),
                    "oi": float(r.iloc[5]) if str(r.iloc[5]) not in ("-", "", "None") else 0,
                }
        except (ValueError, TypeError, IndexError):
            continue
    if best is None:
        return None

    premium = best["call_sell"]
    days = (expire - today).days
    T = days / 365.0
    iv = implied_vol(premium, spot, best["strike"], T)
    otm = (best["strike"] - spot) / spot * 100
    prob = prob_above_strike(spot, best["strike"], T, iv) if iv > 0 else 0
    enh_nominal = annualized_enhancement(premium, spot, days)

    return {
        "symbol": best["symbol"],
        "strike": best["strike"],
        "otm_pct": otm,
        "premium_points": premium,
        "premium_yuan": premium * 200,
        "iv": iv * 100,
        "days_to_expire": days,
        "expire_date": expire.isoformat(),
        "assign_prob": prob * 100,
        "breakeven": best["strike"] + premium,
        "enhancement_nominal": enh_nominal,
        "oi": best["oi"],
        "fetched_at": datetime.now().isoformat(timespec="seconds"),
    }
