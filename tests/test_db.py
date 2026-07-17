# tests/test_db.py
from __future__ import annotations
import unittest
import tempfile
import os
from pathlib import Path
from datetime import datetime

from db import (
    init_db, upsert_valuation, upsert_contract,
    insert_signal, query_latest_valuation, query_valuation_history,
    query_contracts_by_date, query_main_continuous_history,
    load_position, save_position,
    SCHEMA,
)


class TestSchema(unittest.TestCase):
    def test_schema_contains_four_tables(self):
        self.assertIn("daily_valuation", SCHEMA)
        self.assertIn("daily_contracts", SCHEMA)
        self.assertIn("signals", SCHEMA)
        self.assertIn("position", SCHEMA)


class TestPositionCRUD(unittest.TestCase):
    def setUp(self):
        self._fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(self._fd)
        self.conn = init_db(Path(self.path))

    def tearDown(self):
        self.conn.close()
        os.unlink(self.path)

    def test_load_empty_returns_none(self):
        """空表返回 None"""
        self.assertIsNone(load_position(self.conn))

    def test_save_then_load_roundtrip(self):
        """保存后读取应一致"""
        save_position(self.conn, {
            "status": "holding",
            "contract": "IM2608",
            "entry_date": "2026-07-18",
            "entry_price": 7000.0,
            "updated_at": "2026-07-18T10:00:00",
        })
        row = load_position(self.conn)
        self.assertEqual(row["status"], "holding")
        self.assertEqual(row["contract"], "IM2608")
        self.assertEqual(row["entry_date"], "2026-07-18")
        self.assertAlmostEqual(row["entry_price"], 7000.0)

    def test_save_replaces_single_row(self):
        """多次 save 只保留一行（id=1 约束）"""
        save_position(self.conn, {
            "status": "holding", "contract": "IM2608",
            "entry_date": "2026-07-18", "entry_price": 7000.0,
            "updated_at": "2026-07-18T10:00:00",
        })
        save_position(self.conn, {
            "status": "empty", "contract": None,
            "entry_date": None, "entry_price": None,
            "updated_at": "2026-07-20T10:00:00",
        })
        row = load_position(self.conn)
        self.assertEqual(row["status"], "empty")
        self.assertIsNone(row["contract"])
        # 验证只有 1 行
        cnt = self.conn.execute("SELECT COUNT(*) FROM position").fetchone()[0]
        self.assertEqual(cnt, 1)


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
        """query_contracts_by_date 排除 IM0（主力连续）"""
        # 插入 2 个普通合约 + 1 个主力连续
        for sym, ctype in [("IM2607", "当月"), ("IM2608", "下月"), ("IM0", "主力")]:
            upsert_contract(self.conn, {
                "date": "2026-07-10", "symbol": sym, "name": f"中证1000 {sym[-2:]}",
                "contract_type": ctype, "close": 8150.0, "settle": 8145.0,
                "volume": 100000, "open_interest": 50000,
                "expire_date": "2026-07-17", "days_to_expire": 7,
                "basis": -48.31, "annualized_discount": 13.5,
                "fetched_at": "2026-07-12T10:00:00",
            })
        rows = query_contracts_by_date(self.conn, "2026-07-10")
        # 只返回 2 个（排除 IM0）
        self.assertEqual(len(rows), 2)
        symbols = [r["symbol"] for r in rows]
        self.assertNotIn("IM0", symbols)

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

    def test_query_latest_signals_filters_by_date(self):
        """query_latest_signals 按 date 倒序返回，cutoff 日期过滤"""
        from db import query_latest_signals
        # 插入 3 个信号，日期不同
        for d, stype in [("2026-06-01", "wait"), ("2026-07-01", "warn_entry"), ("2026-07-10", "entry")]:
            insert_signal(self.conn, {
                "date": d, "signal_type": stype,
                "condition": "test", "current_value": "test",
                "threshold": "test", "suggestion": "test",
                "created_at": "2026-07-12T10:00:00",
            })
        # query_latest_signals 默认 days=30，cutoff = now - 30 days
        # 由于测试数据是 2026-06/07，需要用足够大的 days 才能查到
        sigs = query_latest_signals(self.conn, days=365)
        # 应返回全部 3 个，按 date DESC 排序
        self.assertEqual(len(sigs), 3)
        self.assertEqual(sigs[0]["date"], "2026-07-10")  # 最新在前
        self.assertEqual(sigs[0]["signal_type"], "entry")


if __name__ == "__main__":
    unittest.main()
