# data_fetcher.py
from __future__ import annotations
import time
from datetime import date, datetime
from typing import Any, Callable

import akshare as ak

from basis import (
    classify_contract, days_to_expire, compute_basis,
    compute_annualized_discount, third_friday,
)
from options import (
    implied_vol, annualized_enhancement, prob_above_strike,
    implied_forward,
)


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


def build_valuation_row(pe_row: dict, pb_row: dict) -> dict:
    """合并 PE 行和 PB 行成一条 daily_valuation 记录。"""
    return {
        "date": str(pe_row["date"])[:10],
        "close": float(pe_row["close"]),
        "pe_static": float(pe_row.get("pe_static", 0)),
        "pe_ttm": float(pe_row.get("pe_ttm", 0)),
        "pe_ttm_eq": float(pe_row.get("pe_ttm_eq", 0)),
        "pe_static_med": float(pe_row.get("pe_static_med", 0)),
        "pe_ttm_med": float(pe_row.get("pe_ttm_med", 0)),
        "pb": float(pb_row.get("pb", 0)),
        "pb_med": float(pb_row.get("pb_med", 0)),
        "pb_w": float(pb_row.get("pb_w", 0)),
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
        "等权静态市盈率": "pe_static_eq", "静态市盈率": "pe_static",
        "静态市盈率中位数": "pe_static_med", "等权滚动市盈率": "pe_ttm_eq",
        "滚动市盈率": "pe_ttm", "滚动市盈率中位数": "pe_ttm_med",
    })
    pb_df = pb_raw.rename(columns={
        "日期": "date", "指数": "close", "市净率": "pb",
        "等权市净率": "pb_w", "市净率中位数": "pb_med",
    })

    merged = pe_df.merge(pb_df[["date", "pb", "pb_med", "pb_w"]], on="date", how="inner")
    merged = merged.sort_values("date").reset_index(drop=True)

    rows = []
    for _, r in merged.iterrows():
        try:
            rows.append(build_valuation_row(r.to_dict(), r.to_dict()))
        except (ValueError, TypeError):
            continue
    return rows


def fetch_main_continuous(spot_close: float, ref_date: date) -> list[dict]:
    """拉主力连续 IM0 全历史。spot_close 用于算基差。
    注：主力连续没有交割日，days_to_expire/expire_date 留空。
    """
    df = retry(lambda: ak.futures_main_sina(symbol="IM0"))
    if df is None:
        return []

    rows = []
    for _, r in df.iterrows():
        try:
            close = float(r["收盘价"])
            d = str(r["日期"])[:10]
            rows.append({
                "date": d,
                "symbol": "IM0",
                "name": "主力连续",
                "contract_type": "主力",
                "close": close,
                "settle": close,  # 新浪主力连续无 settle，用 close
                "volume": float(r.get("成交量", 0)),
                "open_interest": float(r.get("持仓量", 0)),
                "expire_date": None,
                "days_to_expire": None,
                "basis": compute_basis(close, spot_close),
                "annualized_discount": 0.0,  # 主力连续无交割日，无法年化
                "fetched_at": datetime.now().isoformat(timespec="seconds"),
            })
        except (ValueError, TypeError, KeyError):
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
                cl = r.iloc[2]  # call 最新价
                cs = r.iloc[3]  # call 卖价
                pl = r.iloc[11]  # put 最新价
                if str(cl) in ("-", "", "None") or str(cs) in ("-", "", "None"):
                    continue
                best_diff = diff
                best = {
                    "symbol": mo_symbol,
                    "strike": k,
                    "call_last": float(cl),
                    "call_sell": float(cs),
                    "put_last": float(pl) if str(pl) not in ("-", "", "None") else None,
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
    fwd = None
    disc_implied = None
    if best["put_last"] and best["put_last"] > 0:
        fwd = implied_forward(premium, best["put_last"], best["strike"], T)
        disc_implied = (spot - fwd) / spot * 100

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
        "implied_forward": fwd,
        "implied_discount": disc_implied,
        "fetched_at": datetime.now().isoformat(timespec="seconds"),
    }
