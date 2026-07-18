# tests/test_monitor.py
from __future__ import annotations
import unittest
import tempfile
import os
from pathlib import Path
from datetime import date
from unittest.mock import patch

from monitor import (
    _extract_signal_metrics, _target_trade_date, _load_position,
    cmd_open, cmd_close, _compute_expected_return, _window_median,
)
from db import init_db, load_position


def _contract(ctype, disc, days):
    return {
        "contract_type": ctype,
        "annualized_discount": disc,
        "days_to_expire": days,
    }


class TestExtractSignalMetrics(unittest.TestCase):
    """_extract_signal_metrics 同时返回当月贴水（warn 用）和下月贴水（策略判断主指标）。
    不再有 days<switch_days 的 fallback——下月贴水直接对应下月合约。"""

    def setUp(self):
        self.metrics = {
            "pe_ttm_pct": {
                "10y": {"pct": 72.0, "n": 2440, "expected": 2440},
                "5y": {"pct": 80.0, "n": 1220, "expected": 1220},
                "all": {"pct": 60.0, "n": 2900, "expected": None},
            },
            "contracts": [],
        }

    def test_returns_both_near_and_far_discount(self):
        """当月 + 下月都有 → 同时返回两个字段"""
        self.metrics["contracts"] = [
            _contract("当月", 5.0, 20),
            _contract("下月", 7.0, 50),
        ]
        out = _extract_signal_metrics(self.metrics)
        self.assertAlmostEqual(out["current_month_discount"], 5.0)
        self.assertAlmostEqual(out["next_month_discount"], 7.0)
        self.assertEqual(out["current_month_days"], 20)

    def test_far_discount_independent_of_near_days(self):
        """下月贴水独立于当月 days：当月临交割时不再 fallback"""
        self.metrics["contracts"] = [
            _contract("当月", 0.0, 3),   # 临近交割
            _contract("下月", 7.2, 35),
        ]
        out = _extract_signal_metrics(self.metrics)
        # 当月贴水仍按当月数据返回（warn 用）
        self.assertAlmostEqual(out["current_month_discount"], 0.0)
        self.assertEqual(out["current_month_days"], 3)
        # 下月贴水独立返回（策略判断用）
        self.assertAlmostEqual(out["next_month_discount"], 7.2)

    def test_no_next_month_returns_zero_far(self):
        """无下月合约 → next_month_discount = 0（保守）"""
        self.metrics["contracts"] = [_contract("当月", 5.0, 20)]
        out = _extract_signal_metrics(self.metrics)
        self.assertAlmostEqual(out["current_month_discount"], 5.0)
        self.assertAlmostEqual(out["next_month_discount"], 0)

    def test_no_contracts_returns_zeros(self):
        """无合约数据 → 全 0/999（触发 wait 兜底）"""
        out = _extract_signal_metrics(self.metrics)
        self.assertAlmostEqual(out["current_month_discount"], 0)
        self.assertAlmostEqual(out["next_month_discount"], 0)
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


class TestComputeExpectedReturn(unittest.TestCase):
    """三因子预期收益模型：ROE=PB/PE、分红默认 1%、展期=下月贴水、估值回归到 PE 10y 中位数。"""

    def test_roe_derived_from_pb_pe(self):
        """PB/PE = E/B = ROE。PB=2.5, PE=35 → ROE ≈ 7.14%"""
        er = _compute_expected_return(
            close=8198, pe_ttm=35.0, pb=2.5,
            next_month_discount=8.0, pe_median_10y=None)
        self.assertAlmostEqual(er["roe_pct"], 2.5 / 35.0 * 100, places=3)
        self.assertAlmostEqual(er["dividend_yield_pct"], 1.0)
        self.assertAlmostEqual(er["roll_yield_pct"], 8.0)

    def test_compounding_3y_5y(self):
        """估值不变年化 = ROE + 分红 + 贴水；3年/5年复利按 (1+r)^n-1 算"""
        er = _compute_expected_return(
            close=8198, pe_ttm=35.0, pb=2.5,
            next_month_discount=8.0, pe_median_10y=None)
        # base = 7.143 + 1.0 + 8.0 = 16.143
        expected_base = 2.5 / 35.0 * 100 + 1.0 + 8.0
        self.assertAlmostEqual(er["annual_no_valuation_pct"], expected_base, places=2)
        expected_c3 = ((1 + expected_base / 100) ** 3 - 1) * 100
        expected_c5 = ((1 + expected_base / 100) ** 5 - 1) * 100
        self.assertAlmostEqual(er["c3y_no_valuation_pct"], expected_c3, places=2)
        self.assertAlmostEqual(er["c5y_no_valuation_pct"], expected_c5, places=2)

    def test_valuation_reversion_positive_when_undervalued(self):
        """PE 低于 10 年中位数 → 估值回归正值（有修复空间）"""
        er = _compute_expected_return(
            close=8198, pe_ttm=30.0, pb=2.4,
            next_month_discount=10.0, pe_median_10y=40.0)
        # (40-30)/30 = +33.3%
        self.assertAlmostEqual(er["valuation_change_pct"], (40 - 30) / 30 * 100,
                               places=2)
        self.assertGreater(er["valuation_change_pct"], 0)
        # 含估值回归 1 年预期 > 估值不变年化
        self.assertGreater(er["annual_with_mean_reversion_pct"],
                           er["annual_no_valuation_pct"])

    def test_valuation_reversion_negative_when_overvalued(self):
        """PE 高于 10 年中位数 → 估值回归负值（有回落风险）"""
        er = _compute_expected_return(
            close=8198, pe_ttm=45.0, pb=2.8,
            next_month_discount=5.0, pe_median_10y=35.0)
        # (35-45)/45 = -22.2%
        self.assertLess(er["valuation_change_pct"], 0)
        self.assertLess(er["annual_with_mean_reversion_pct"],
                        er["annual_no_valuation_pct"])

    def test_no_pe_median_zero_reversion(self):
        """无中位数（样本不足）→ 估值回归 0，含回归 == 估值不变"""
        er = _compute_expected_return(
            close=8198, pe_ttm=35.0, pb=2.5,
            next_month_discount=8.0, pe_median_10y=None)
        self.assertEqual(er["valuation_change_pct"], 0.0)
        self.assertEqual(er["annual_with_mean_reversion_pct"],
                         er["annual_no_valuation_pct"])

    def test_negative_discount_reduces_return(self):
        """远月升水（贴水为负）→ 展期收益为负，拉低整体预期"""
        er = _compute_expected_return(
            close=8198, pe_ttm=35.0, pb=2.5,
            next_month_discount=-2.0, pe_median_10y=None)
        self.assertLess(er["roll_yield_pct"], 0)
        # base = ROE + 分红 - 2
        self.assertLess(er["annual_no_valuation_pct"],
                        2.5 / 35.0 * 100 + 1.0)


class TestWindowMedian(unittest.TestCase):
    """PE 10 年中位数计算：cutoff 用今日午夜。"""

    def test_basic_median(self):
        from datetime import datetime, timedelta
        today = datetime.now().strftime("%Y-%m-%d")
        history = [
            {"date": today, "pe_ttm": 30.0},
            {"date": today, "pe_ttm": 40.0},
            {"date": today, "pe_ttm": 50.0},
        ]
        # 这三个值都在 10y 窗口内
        median = _window_median(history, "pe_ttm", days=3652)
        self.assertEqual(median, 40.0)

    def test_empty_history_returns_none(self):
        self.assertIsNone(_window_median([], "pe_ttm", days=3652))

    def test_filters_old_dates(self):
        """10y+1 天前的数据应被过滤"""
        from datetime import datetime, timedelta
        old = (datetime.now() - timedelta(days=4000)).strftime("%Y-%m-%d")
        today = datetime.now().strftime("%Y-%m-%d")
        history = [
            {"date": old, "pe_ttm": 999.0},  # 应被过滤
            {"date": today, "pe_ttm": 30.0},
            {"date": today, "pe_ttm": 40.0},
        ]
        median = _window_median(history, "pe_ttm", days=3652)
        self.assertEqual(median, 35.0)  # (30+40)/2


if __name__ == "__main__":
    unittest.main()
