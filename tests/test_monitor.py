# tests/test_monitor.py
from __future__ import annotations
import unittest
from datetime import date
from unittest.mock import patch

from monitor import _extract_signal_metrics, _target_trade_date


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


if __name__ == "__main__":
    unittest.main()
