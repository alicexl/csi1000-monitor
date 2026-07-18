# tests/test_data_fetcher.py
from __future__ import annotations
import unittest
from unittest.mock import patch, MagicMock
from datetime import date
import pandas as pd

from data_fetcher import (
    retry, fetch_valuation, fetch_main_continuous,
    fetch_daily_contracts, build_valuation_row,
)


class TestRetry(unittest.TestCase):
    def test_succeeds_first_try(self):
        calls = []
        def fn():
            calls.append(1)
            return "ok"
        self.assertEqual(retry(fn, retries=3, delays=(0.01, 0.01, 0.01)), "ok")
        self.assertEqual(len(calls), 1)

    def test_retries_then_succeeds(self):
        calls = []
        def fn():
            calls.append(1)
            if len(calls) < 3:
                raise ConnectionError("fail")
            return "ok"
        self.assertEqual(retry(fn, retries=3, delays=(0.01, 0.01, 0.01)), "ok")
        self.assertEqual(len(calls), 3)

    def test_all_retries_fail_returns_none(self):
        def fn():
            raise ConnectionError("always fail")
        self.assertIsNone(retry(fn, retries=2, delays=(0.01, 0.01)))


class TestBuildValuationRow(unittest.TestCase):
    def test_merge_pe_pb(self):
        """merged row（PE + PB 已 join）构造 valuation 记录"""
        row = build_valuation_row({
            "date": "2026-07-10", "close": 8198.31,
            "pe_static": 35.77, "pe_ttm": 34.57, "pe_ttm_eq": 61.05,
            "pe_static_med": 41.55, "pe_ttm_med": 40.91,
            "pb": 2.58, "pb_med": 2.65, "pb_w": 4.77,
        })
        self.assertEqual(row["date"], "2026-07-10")
        self.assertAlmostEqual(row["close"], 8198.31)
        self.assertAlmostEqual(row["pe_ttm"], 34.57)
        self.assertAlmostEqual(row["pb"], 2.58)
        self.assertIn("fetched_at", row)


class TestFetchValuation(unittest.TestCase):
    @patch("data_fetcher.ak")
    def test_fetch_and_merge(self, mock_ak):
        """mock akshare 返回 PE/PB DataFrame → 返回合并行列表"""
        mock_ak.stock_index_pe_lg.return_value = pd.DataFrame([
            {"日期": "2026-07-09", "指数": 8300.08, "等权静态市盈率": 64.85,
             "静态市盈率": 36.20, "静态市盈率中位数": 41.48,
             "等权滚动市盈率": 61.37, "滚动市盈率": 35.00, "滚动市盈率中位数": 40.39},
            {"日期": "2026-07-10", "指数": 8198.31, "等权静态市盈率": 65.25,
             "静态市盈率": 35.77, "静态市盈率中位数": 41.55,
             "等权滚动市盈率": 61.05, "滚动市盈率": 34.57, "滚动市盈率中位数": 40.91},
        ])
        mock_ak.stock_index_pb_lg.return_value = pd.DataFrame([
            {"日期": "2026-07-09", "指数": 8300.08, "市净率": 2.62, "等权市净率": 4.82, "市净率中位数": 2.66},
            {"日期": "2026-07-10", "指数": 8198.31, "市净率": 2.58, "等权市净率": 4.77, "市净率中位数": 2.65},
        ])
        rows = fetch_valuation()
        self.assertEqual(len(rows), 2)
        self.assertAlmostEqual(rows[-1]["pe_ttm"], 34.57)
        self.assertAlmostEqual(rows[-1]["pb"], 2.58)


class TestFetchMainContinuous(unittest.TestCase):
    @patch("data_fetcher.ak")
    def test_returns_im0_rows(self, mock_ak):
        mock_ak.futures_main_sina.return_value = pd.DataFrame([
            {"日期": "2026-07-10", "收盘价": 8150, "开盘价": 8100,
             "最高价": 8200, "最低价": 8050, "成交量": 100000, "持仓量": 50000},
        ])
        rows = fetch_main_continuous(
            spot_by_date={"2026-07-10": 8198.31}, ref_date=date(2026, 7, 10))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["symbol"], "IM0")
        self.assertEqual(rows[0]["contract_type"], "主力")
        # 基差用对应日期的现货算，不是 None
        self.assertAlmostEqual(rows[0]["basis"], 8150 - 8198.31)

    @patch("data_fetcher.ak")
    def test_missing_spot_yields_none_basis(self, mock_ak):
        """现货映射缺失该日期时，basis=None（后续分位计算过滤掉）"""
        mock_ak.futures_main_sina.return_value = pd.DataFrame([
            {"日期": "2026-07-10", "收盘价": 8150, "开盘价": 8100,
             "最高价": 8200, "最低价": 8050, "成交量": 100000, "持仓量": 50000},
        ])
        rows = fetch_main_continuous(
            spot_by_date={}, ref_date=date(2026, 7, 10))
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0]["basis"])


class TestFetchDailyContracts(unittest.TestCase):
    @patch("data_fetcher.ak")
    def test_filter_im_contracts(self, mock_ak):
        """只保留 IM 开头合约"""
        mock_ak.get_futures_daily.return_value = pd.DataFrame([
            {"symbol": "IM2607", "date": "20260710", "open": 8100, "high": 8200,
             "low": 8050, "close": 8150, "volume": 100000, "open_interest": 50000,
             "turnover": 1000000, "settle": 8145, "pre_settle": 8100, "variety": "IM"},
            {"symbol": "IF2607", "date": "20260710", "open": 3900, "high": 3950,
             "low": 3850, "close": 3900, "volume": 50000, "open_interest": 30000,
             "turnover": 500000, "settle": 3900, "pre_settle": 3890, "variety": "IF"},
            {"symbol": "IM2608", "date": "20260710", "open": 8050, "high": 8150,
             "low": 8000, "close": 8098, "volume": 80000, "open_interest": 40000,
             "turnover": 800000, "settle": 8090, "pre_settle": 8050, "variety": "IM"},
        ])
        rows = fetch_daily_contracts(date(2026, 7, 10), spot_close=8198.31)
        symbols = [r["symbol"] for r in rows]
        self.assertIn("IM2607", symbols)
        self.assertIn("IM2608", symbols)
        self.assertNotIn("IF2607", symbols)


if __name__ == "__main__":
    unittest.main()
