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
            "pe_static": 35.77, "pe_ttm": 34.57,
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
            "pe_static": 35.77, "pe_ttm": 34.57,
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
        self.assertEqual(latest["fetched_at"], "2026-07-12T20:00:00")
        # 行数仍是 1（不是新增）
        cnt = self.conn.execute(
            "SELECT COUNT(*) FROM daily_valuation WHERE date = '2026-07-10'"
        ).fetchone()[0]
        self.assertEqual(cnt, 1)

    def test_query_history(self):
        for d, close in [("2026-07-08", 8117.86), ("2026-07-09", 8300.08), ("2026-07-10", 8198.31)]:
            upsert_valuation(self.conn, {
                "date": d, "close": close,
                "pe_static": 35.0, "pe_ttm": 34.0,
                "pb": 2.5,
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
        results = {}
        for sym, ctype in [("IM2607", "当月"), ("IM2608", "下月"), ("IM0", "主力")]:
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
        # 只返回 2 个（排除 IM0）
        self.assertEqual(len(rows), 2)
        symbols = [r["symbol"] for r in rows]
        self.assertNotIn("IM0", symbols)

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
        self.assertEqual(rows[0]["fetched_at"], "2026-07-12T20:00:00")

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


class TestMainContinuousBasisMigration(unittest.TestCase):
    """老 DB 的 IM0 basis 用了今日现货（bug），迁移到 per-date 现货。"""

    def setUp(self):
        self._fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(self._fd)

    def tearDown(self):
        if hasattr(self, "conn"):
            self.conn.close()
        os.unlink(self.path)

    def _seed_old_db_with_bug(self):
        """直接用低层 sqlite3 建 DB + 灌 buggy 数据，模拟未迁移的老库。"""
        import sqlite3
        conn = sqlite3.connect(self.path)
        conn.executescript(SCHEMA)
        # 估值：2026-07-08 现货 8000，2026-07-10 现货 8170
        for d, c in [("2026-07-08", 8000.0), ("2026-07-10", 8170.0)]:
            conn.execute(
                "INSERT INTO daily_valuation (date, close, pe_static, pe_ttm, "
                "pb, fetched_at) "
                "VALUES (?, ?, 0, 0, 0, 't')",
                (d, c),
            )
        # buggy IM0：旧 fetch 用 today_spot=8198 算所有历史行
        for d, close in [("2026-07-08", 7950), ("2026-07-10", 8150)]:
            conn.execute(
                "INSERT INTO daily_contracts (date, symbol, name, contract_type, "
                "close, settle, volume, open_interest, expire_date, days_to_expire, "
                "basis, annualized_discount, fetched_at) "
                "VALUES (?, 'IM0', '主力连续', '主力', ?, ?, 0, 0, NULL, NULL, ?, NULL, 't')",
                (d, close, close, close - 8198),
            )
        conn.commit()
        conn.close()

    def test_migration_recomputes_basis(self):
        self._seed_old_db_with_bug()
        self.conn = init_db(Path(self.path))  # 触发迁移
        hist = query_main_continuous_history(self.conn, days=10)
        by_date = {r["date"]: r["basis"] for r in hist}
        # 修复后：2026-07-08 → 7950 - 8000 = -50（原 buggy 值 -248）
        self.assertAlmostEqual(by_date["2026-07-08"], -50.0)
        # 2026-07-10 → 8150 - 8170 = -20（原 buggy 值 -48）
        self.assertAlmostEqual(by_date["2026-07-10"], -20.0)

    def test_migration_is_idempotent(self):
        self._seed_old_db_with_bug()
        self.conn = init_db(Path(self.path))
        self.conn.close()
        # 第二次 init 不应再改动（user_version 已 = 1）
        self.conn = init_db(Path(self.path))
        hist = query_main_continuous_history(self.conn, days=10)
        by_date = {r["date"]: r["basis"] for r in hist}
        self.assertAlmostEqual(by_date["2026-07-08"], -50.0)
        self.assertAlmostEqual(by_date["2026-07-10"], -20.0)

    def test_migration_skips_rows_without_valuation(self):
        """估值表缺该日期时，basis 保持原值（不强行覆盖为 NULL）"""
        import sqlite3
        conn = sqlite3.connect(self.path)
        conn.executescript(SCHEMA)
        conn.execute(
            "INSERT INTO daily_valuation (date, close, pe_static, pe_ttm, "
            "pb, fetched_at) "
            "VALUES ('2026-07-10', 8170.0, 0, 0, 0, 't')"
        )
        # 2026-07-08 没有估值行
        for d, close in [("2026-07-08", 7950), ("2026-07-10", 8150)]:
            conn.execute(
                "INSERT INTO daily_contracts (date, symbol, name, contract_type, "
                "close, settle, volume, open_interest, expire_date, days_to_expire, "
                "basis, annualized_discount, fetched_at) "
                "VALUES (?, 'IM0', '主力连续', '主力', ?, ?, 0, 0, NULL, NULL, ?, NULL, 't')",
                (d, close, close, close - 8198),
            )
        conn.commit()
        conn.close()
        self.conn = init_db(Path(self.path))
        hist = query_main_continuous_history(self.conn, days=10)
        by_date = {r["date"]: r["basis"] for r in hist}
        # 有估值 → 重算
        self.assertAlmostEqual(by_date["2026-07-10"], -20.0)
        # 无估值 → 保持原 buggy 值（-248）
        self.assertAlmostEqual(by_date["2026-07-08"], -248.0)


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

    def test_migrate_signals_dedup_old_schema(self):
        """老 schema（无 UNIQUE 约束）的 DB 重新 init_db 时自动清理重复"""
        # 先关闭现有连接，用裸 sqlite3 建一个老 schema 的 DB
        self.conn.close()
        import sqlite3 as sq
        raw = sq.connect(self.path)
        raw.executescript("""
            DROP TABLE IF EXISTS signals;
            CREATE TABLE signals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date TEXT NOT NULL,
                signal_type TEXT NOT NULL,
                condition TEXT,
                current_value TEXT,
                threshold TEXT,
                suggestion TEXT,
                created_at TEXT NOT NULL
            );
        """)
        # 插 4 行，其中 (2026-07-17, wait, X) 重复 3 次
        for i in range(3):
            raw.execute(
                "INSERT INTO signals (date, signal_type, condition, current_value, "
                "threshold, suggestion, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                ("2026-07-17", "wait", "PE 70%", "v", "t", "s", f"2026-07-18T0{i}:00:00"),
            )
        raw.execute(
            "INSERT INTO signals (date, signal_type, condition, current_value, "
            "threshold, suggestion, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("2026-07-17", "entry", "PE<50", "v", "t", "s", "2026-07-18T01:00:00"),
        )
        raw.commit()
        raw.close()
        # 重新 init_db 触发 migration
        self.conn = init_db(Path(self.path))
        rows = self.conn.execute(
            "SELECT date, signal_type, condition FROM signals ORDER BY id"
        ).fetchall()
        # 原 4 行去重后剩 2 行
        self.assertEqual(len(rows), 2)
        # 保留最小 id 的那条
        types = [r["signal_type"] for r in rows]
        self.assertIn("wait", types)
        self.assertIn("entry", types)
        # 验证 unique index 已建
        idx = self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='signals'"
        ).fetchall()
        idx_names = {r["name"] for r in idx}
        self.assertIn("idx_signals_dedup", idx_names)


if __name__ == "__main__":
    unittest.main()
