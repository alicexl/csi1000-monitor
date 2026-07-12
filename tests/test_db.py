# tests/test_db.py
from __future__ import annotations
import unittest
import tempfile
import os
from pathlib import Path
from datetime import datetime

from db import (
    get_conn, init_db, upsert_valuation, upsert_contract,
    insert_signal, query_latest_valuation, query_valuation_history,
    query_contracts_by_date, query_main_continuous_history,
    SCHEMA,
)


class TestSchema(unittest.TestCase):
    def test_schema_contains_three_tables(self):
        self.assertIn("daily_valuation", SCHEMA)
        self.assertIn("daily_contracts", SCHEMA)
        self.assertIn("signals", SCHEMA)


class TestValuationCRUD(unittest.TestCase):
    def setUp(self):
        self._fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(self._fd)
        self.conn = init_db(Path(self.path))

    def tearDown(self):
        self.conn.close()
        os.unlink(self.path)

    def test_upsert_and_query_latest(self):
        row = {
            "date": "2026-07-10", "close": 8198.31,
            "pe_static": 35.77, "pe_ttm": 34.57, "pe_ttm_eq": 61.05,
            "pe_static_med": 41.55, "pe_ttm_med": 40.91,
            "pb": 2.58, "pb_med": 2.65, "pb_w": 4.77,
            "fetched_at": "2026-07-12T10:00:00",
        }
        changed = upsert_valuation(self.conn, row)
        self.assertTrue(changed)

        latest = query_latest_valuation(self.conn)
        self.assertIsNotNone(latest)
        self.assertEqual(latest["date"], "2026-07-10")
        self.assertAlmostEqual(latest["close"], 8198.31, places=2)

    def test_upsert_duplicate_returns_false(self):
        """重复插入同一天返回 False（PK 去重）"""
        row = {
            "date": "2026-07-10", "close": 8198.31,
            "pe_static": 35.77, "pe_ttm": 34.57, "pe_ttm_eq": 61.05,
            "pe_static_med": 41.55, "pe_ttm_med": 40.91,
            "pb": 2.58, "pb_med": 2.65, "pb_w": 4.77,
            "fetched_at": "2026-07-12T10:00:00",
        }
        upsert_valuation(self.conn, row)
        changed = upsert_valuation(self.conn, row)
        self.assertFalse(changed)

    def test_query_history(self):
        for d, close in [("2026-07-08", 8117.86), ("2026-07-09", 8300.08), ("2026-07-10", 8198.31)]:
            upsert_valuation(self.conn, {
                "date": d, "close": close,
                "pe_static": 35.0, "pe_ttm": 34.0, "pe_ttm_eq": 60.0,
                "pe_static_med": 41.0, "pe_ttm_med": 40.0,
                "pb": 2.5, "pb_med": 2.6, "pb_w": 4.7,
                "fetched_at": "2026-07-12T10:00:00",
            })
        hist = query_valuation_history(self.conn, days=10)
        self.assertEqual(len(hist), 3)
        self.assertEqual(hist[-1]["close"], 8198.31)


class TestContractCRUD(unittest.TestCase):
    def setUp(self):
        self._fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(self._fd)
        self.conn = init_db(Path(self.path))

    def tearDown(self):
        self.conn.close()
        os.unlink(self.path)

    def test_upsert_and_query_by_date(self):
        for sym, ctype in [("IM2607", "当月"), ("IM2608", "下月")]:
            upsert_contract(self.conn, {
                "date": "2026-07-10", "symbol": sym, "name": f"中证1000 {sym[-2:]}",
                "contract_type": ctype, "close": 8150.0, "settle": 8145.0,
                "volume": 100000, "open_interest": 50000,
                "expire_date": "2026-07-17", "days_to_expire": 7,
                "basis": -48.31, "annualized_discount": 13.5,
                "fetched_at": "2026-07-12T10:00:00",
            })
        rows = query_contracts_by_date(self.conn, "2026-07-10")
        self.assertEqual(len(rows), 2)

    def test_main_continuous_history(self):
        """主力连续合约 symbol='IM0' 单独查询"""
        for d, close in [("2026-07-08", 8100), ("2026-07-09", 8280), ("2026-07-10", 8170)]:
            upsert_contract(self.conn, {
                "date": d, "symbol": "IM0", "name": "主力连续",
                "contract_type": "主力", "close": close, "settle": close,
                "volume": 200000, "open_interest": 120000,
                "expire_date": None, "days_to_expire": None,
                "basis": -50, "annualized_discount": 8.0,
                "fetched_at": "2026-07-12T10:00:00",
            })
        hist = query_main_continuous_history(self.conn, days=10)
        self.assertEqual(len(hist), 3)


class TestSignalCRUD(unittest.TestCase):
    def setUp(self):
        self._fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(self._fd)
        self.conn = init_db(Path(self.path))

    def tearDown(self):
        self.conn.close()
        os.unlink(self.path)

    def test_insert_and_autoincrement(self):
        sid1 = insert_signal(self.conn, {
            "date": "2026-07-10", "signal_type": "wait",
            "condition": "PE分位 > 50", "current_value": "81.8%",
            "threshold": "<50%", "suggestion": "继续等待",
            "created_at": "2026-07-12T10:00:00",
        })
        sid2 = insert_signal(self.conn, {
            "date": "2026-07-11", "signal_type": "entry",
            "condition": "PE<50 且 贴水>5", "current_value": "45%, 8%",
            "threshold": "<50%, >5%", "suggestion": "买入当月",
            "created_at": "2026-07-12T10:00:00",
        })
        self.assertEqual(sid1, 1)
        self.assertEqual(sid2, 2)


if __name__ == "__main__":
    unittest.main()
