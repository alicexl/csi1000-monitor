# data_fetcher.py
from __future__ import annotations
import time
from datetime import date, datetime
from typing import Any, Callable

import akshare as ak

from basis import (
    classify_contract, days_to_expire, compute_basis,
    compute_annualized_discount,
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
        "加权市净率": "pb_w", "市净率中位数": "pb_med",
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
