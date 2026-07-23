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
    query_contracts_by_date,
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
            "pe_ttm": 34.57,
            "pb": 2.58,
            "fetched_at": "2026-07-12T10:00:00",
        }
        result = upsert_valuation(self.conn, row)
        self.assertEqual(result, "inserted")

        latest = query_latest_valuation(self.conn)
        self.assertIsNotNone(latest)
        self.assertEqual(latest["date"], "2026-07-10")
        self.assertAlmostEqual(latest["close"], 8198.31, places=2)

    def test_upsert_updates_existing_row(self):
        """重复插入同一天 → updated，且数值被新值覆盖"""
        row = {
            "date": "2026-07-10", "close": 8198.31,
            "pe_ttm": 34.57,
            "pb": 2.58,
            "fetched_at": "2026-07-12T10:00:00",
        }
        upsert_valuation(self.conn, row)
        # 数据源修正了 close
        row2 = dict(row)
        row2["close"] = 8250.0
        row2["pe_ttm"] = 34.80
        row2["fetched_at"] = "2026-07-12T20:00:00"
        result = upsert_valuation(self.conn, row2)
        self.assertEqual(result, "updated")
        # 验证新值已覆盖
        latest = query_latest_valuation(self.conn)
        self.assertAlmostEqual(latest["close"], 8250.0, places=2)
        self.assertAlmostEqual(latest["pe_ttm"], 34.80, places=2)
        # fetched_at 不随 update 刷新，保留首次入库时间
        self.assertEqual(latest["fetched_at"], "2026-07-12T10:00:00")
        # 行数仍是 1（不是新增）
        cnt = self.conn.execute(
            "SELECT COUNT(*) FROM daily_valuation WHERE date = '2026-07-10'"
        ).fetchone()[0]
        self.assertEqual(cnt, 1)

    def test_query_history(self):
        for d, close in [("2026-07-08", 8117.86), ("2026-07-09", 8300.08), ("2026-07-10", 8198.31)]:
            upsert_valuation(self.conn, {
                "date": d, "close": close,
                "pe_ttm": 34.0,
                "pb": 2.5,
                "fetched_at": "2026-07-12T10:00:00",
            })
        hist = query_valuation_history(self.conn, max_rows=10)
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
        """query_contracts_by_date 返回当日全部合约（按 symbol 升序）"""
        results = {}
        for sym, ctype in [("IM2607", "当月"), ("IM2608", "下月")]:
            results[sym] = upsert_contract(self.conn, {
                "date": "2026-07-10", "symbol": sym, "name": f"中证1000 {sym[-2:]}",
                "contract_type": ctype, "close": 8150.0, "settle": 8145.0,
                "volume": 100000, "open_interest": 50000,
                "expire_date": "2026-07-17", "days_to_expire": 7,
                "basis": -48.31, "annualized_discount": 13.5,
                "fetched_at": "2026-07-12T10:00:00",
            })
        # 首次都是 inserted
        self.assertEqual(set(results.values()), {"inserted"})
        rows = query_contracts_by_date(self.conn, "2026-07-10")
        self.assertEqual(len(rows), 2)
        self.assertEqual([r["symbol"] for r in rows], ["IM2607", "IM2608"])

    def test_upsert_contract_updates_values(self):
        """同一 (date, symbol) 重复插入 → updated，数值被覆盖"""
        row = {
            "date": "2026-07-10", "symbol": "IM2607", "name": "中证1000 07",
            "contract_type": "当月", "close": 8150.0, "settle": 8145.0,
            "volume": 100000, "open_interest": 50000,
            "expire_date": "2026-07-17", "days_to_expire": 7,
            "basis": -48.31, "annualized_discount": 13.5,
            "fetched_at": "2026-07-12T10:00:00",
        }
        self.assertEqual(upsert_contract(self.conn, dict(row)), "inserted")
        # 盘后数据修正
        row2 = dict(row)
        row2["close"] = 8180.0
        row2["basis"] = -18.31
        row2["fetched_at"] = "2026-07-12T20:00:00"
        self.assertEqual(upsert_contract(self.conn, row2), "updated")
        rows = query_contracts_by_date(self.conn, "2026-07-10")
        self.assertEqual(len(rows), 1)
        self.assertAlmostEqual(rows[0]["close"], 8180.0)
        self.assertAlmostEqual(rows[0]["basis"], -18.31)
        # fetched_at 不随 update 刷新，保留首次入库时间
        self.assertEqual(rows[0]["fetched_at"], "2026-07-12T10:00:00")

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

    def test_insert_duplicate_signal_ignored(self):
        """同一 (date, signal_type, condition) 第二次插入被忽略，不产生重复"""
        row = {
            "date": "2026-07-10", "signal_type": "wait",
            "condition": "PE 70%", "current_value": "70%",
            "threshold": "<50%", "suggestion": "继续等待",
            "created_at": "2026-07-12T10:00:00",
        }
        sid1 = insert_signal(self.conn, dict(row))
        sid2 = insert_signal(self.conn, dict(row))
        # 第一次插入成功，第二次命中 UNIQUE 冲突被忽略
        self.assertGreater(sid1, 0)
        cnt = self.conn.execute("SELECT COUNT(*) FROM signals").fetchone()[0]
        self.assertEqual(cnt, 1)

    def test_different_condition_same_day_coexist(self):
        """同一天不同 condition（如 reduce_pe + reduce_basis）都保留"""
        for cond, sugg in [("PE > 85%", "估值过高"), ("贴水 ≤ 0", "升水失效")]:
            insert_signal(self.conn, {
                "date": "2026-07-10", "signal_type": "reduce",
                "condition": cond, "current_value": "test",
                "threshold": "test", "suggestion": sugg,
                "created_at": "2026-07-12T10:00:00",
            })
        cnt = self.conn.execute("SELECT COUNT(*) FROM signals").fetchone()[0]
        self.assertEqual(cnt, 2)


if __name__ == "__main__":
    unittest.main()
