# tests/test_monitor.py
from __future__ import annotations
import unittest
import tempfile
import os
from pathlib import Path
from datetime import date
from unittest.mock import patch

from monitor import _extract_signal_metrics, _target_trade_date, _load_position, cmd_open, cmd_close
from db import init_db, load_position


def _contract(ctype, disc, days):
    return {
        "contract_type": ctype,
        "annualized_discount": disc,
        "days_to_expire": days,
    }


class TestExtractSignalMetrics(unittest.TestCase):
    def setUp(self):
        self.metrics = {
            "pe_ttm_pct": {"10y": 72.0, "5y": 80.0, "all": 60.0},
            "contracts": [],
        }

    def test_use_current_month_when_far_from_expire(self):
        """当月剩余 ≥ switch_days → 用当月"""
        self.metrics["contracts"] = [
            _contract("当月", 5.0, 20),
            _contract("下月", 7.0, 50),
        ]
        out = _extract_signal_metrics(self.metrics, switch_days=7)
        self.assertAlmostEqual(out["current_month_discount"], 5.0)
        self.assertEqual(out["current_month_days"], 20)

    def test_fallback_to_next_month_when_current_near_expire(self):
        """当月剩余 < switch_days → fallback 到下月"""
        self.metrics["contracts"] = [
            _contract("当月", 0.0, 3),   # 临近交割
            _contract("下月", 7.2, 35),
        ]
        out = _extract_signal_metrics(self.metrics, switch_days=7)
        self.assertAlmostEqual(out["current_month_discount"], 7.2)
        self.assertEqual(out["current_month_days"], 35)

    def test_fallback_on_expiry_day(self):
        """当月已交割（days=0）→ fallback 到下月"""
        self.metrics["contracts"] = [
            _contract("当月", 0.0, 0),
            _contract("下月", 7.2, 35),
        ]
        out = _extract_signal_metrics(self.metrics, switch_days=7)
        self.assertAlmostEqual(out["current_month_discount"], 7.2)

    def test_no_fallback_when_no_next_month(self):
        """当月临近交割但无下月数据 → 继续用当月（不丢失信息）"""
        self.metrics["contracts"] = [_contract("当月", 0.0, 3)]
        out = _extract_signal_metrics(self.metrics, switch_days=7)
        self.assertAlmostEqual(out["current_month_discount"], 0.0)

    def test_no_contracts_returns_zero(self):
        """无合约数据 → 贴水 0、天数 999（触发 wait 兜底）"""
        out = _extract_signal_metrics(self.metrics, switch_days=7)
        self.assertAlmostEqual(out["current_month_discount"], 0)
        self.assertEqual(out["current_month_days"], 999)


class TestTargetTradeDate(unittest.TestCase):
    def _mocked(self, y, m, d):
        return patch("monitor.date", wraps=date)

    def test_weekday_returns_self(self):
        """工作日 → 当天"""
        with patch("monitor.date") as mock_date:
            mock_date.today.return_value = date(2026, 7, 17)  # Friday
            mock_date.side_effect = lambda *a, **k: date(*a, **k)
            self.assertEqual(_target_trade_date(), date(2026, 7, 17))

    def test_saturday_falls_back_to_friday(self):
        """周六 → 回退到周五"""
        with patch("monitor.date") as mock_date:
            mock_date.today.return_value = date(2026, 7, 18)  # Saturday
            mock_date.side_effect = lambda *a, **k: date(*a, **k)
            self.assertEqual(_target_trade_date(), date(2026, 7, 17))

    def test_sunday_falls_back_to_friday(self):
        """周日 → 回退到周五"""
        with patch("monitor.date") as mock_date:
            mock_date.today.return_value = date(2026, 7, 19)  # Sunday
            mock_date.side_effect = lambda *a, **k: date(*a, **k)
            self.assertEqual(_target_trade_date(), date(2026, 7, 17))


class TestPositionPersistence(unittest.TestCase):
    """cmd_open / cmd_close / _load_position 集成测试（隔离 DB_PATH）"""

    def setUp(self):
        self._fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(self._fd)
        self._db_patch = patch("monitor.DB_PATH", Path(self.path))
        self._db_patch.start()

    def tearDown(self):
        self._db_patch.stop()
        if os.path.exists(self.path):
            os.unlink(self.path)

    def test_load_empty_returns_default(self):
        """空表 → 默认 Position(status='empty')"""
        conn = init_db(Path(self.path))
        pos = _load_position(conn)
        self.assertEqual(pos.status, "empty")
        self.assertIsNone(pos.contract)
        conn.close()

    def test_cmd_open_then_load(self):
        """open IM2608 7000 → load 出来字段一致"""
        class Args:
            contract = "IM2608"
            entry_price = 7000.0
            entry_date = "2026-07-18"
        rc = cmd_open(Args())
        self.assertEqual(rc, 0)
        conn = init_db(Path(self.path))
        pos = _load_position(conn)
        self.assertEqual(pos.status, "holding")
        self.assertEqual(pos.contract, "IM2608")
        self.assertAlmostEqual(pos.entry_price, 7000.0)
        self.assertEqual(pos.entry_date, "2026-07-18")
        conn.close()

    def test_cmd_close_clears_position(self):
        """open 后 close → 回到 empty"""
        class ArgsOpen:
            contract = "IM2608"
            entry_price = 7000.0
            entry_date = "2026-07-18"
        cmd_open(ArgsOpen())
        rc = cmd_close(None)
        self.assertEqual(rc, 0)
        conn = init_db(Path(self.path))
        pos = _load_position(conn)
        self.assertEqual(pos.status, "empty")
        self.assertIsNone(pos.contract)
        conn.close()

    def test_open_replaces_previous(self):
        """第二次 open 覆盖第一次"""
        class Args1:
            contract = "IM2608"
            entry_price = 7000.0
            entry_date = "2026-07-18"
        class Args2:
            contract = "IM2609"
            entry_price = 6800.0
            entry_date = "2026-08-15"
        cmd_open(Args1())
        cmd_open(Args2())
        conn = init_db(Path(self.path))
        pos = _load_position(conn)
        self.assertEqual(pos.contract, "IM2609")
        self.assertAlmostEqual(pos.entry_price, 6800.0)
        conn.close()


if __name__ == "__main__":
    unittest.main()
