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
    """_extract_signal_metrics 返回当月贴水、下月贴水、roll_yield（= 下月 - 当月）。
    策略判断基于 roll_yield（曲线斜率），贴水作展示参考。"""

    def setUp(self):
        self.metrics = {
            "pe_ttm_pct": {
                "10y": {"pct": 72.0, "n": 2440},
                "5y": {"pct": 80.0, "n": 1220},
                "all": {"pct": 60.0, "n": 2900},
            },
            "contracts": [],
        }

    def test_returns_roll_yield_and_discounts(self):
        """当月 + 下月都有 → 返回 d_near / d_far / roll_yield / days"""
        self.metrics["contracts"] = [
            _contract("当月", 5.0, 20),
            _contract("下月", 7.0, 50),
        ]
        out = _extract_signal_metrics(self.metrics)
        self.assertAlmostEqual(out["current_month_discount"], 5.0)
        self.assertAlmostEqual(out["next_month_discount"], 7.0)
        self.assertEqual(out["current_month_days"], 20)
        # roll_yield = 下月 - 当月（曲线斜率）
        self.assertAlmostEqual(out["roll_yield"], 2.0)

    def test_roll_yield_negative_when_curve_inverted(self):
        """曲线倒挂（near > far）→ roll_yield < 0"""
        self.metrics["contracts"] = [
            _contract("当月", 7.2, 35),
            _contract("下月", 0.0, 3),   # 实际不会发生，仅测算法
        ]
        out = _extract_signal_metrics(self.metrics)
        self.assertAlmostEqual(out["roll_yield"], 0.0 - 7.2)

    def test_no_next_month_roll_yield_negative(self):
        """无下月合约 → next_month_discount = 0，roll_yield = -near"""
        self.metrics["contracts"] = [_contract("当月", 5.0, 20)]
        out = _extract_signal_metrics(self.metrics)
        self.assertAlmostEqual(out["current_month_discount"], 5.0)
        self.assertAlmostEqual(out["next_month_discount"], 0)
        self.assertAlmostEqual(out["roll_yield"], -5.0)

    def test_no_contracts_returns_zeros(self):
        """无合约数据 → 全 0/999（触发 wait 兜底）"""
        out = _extract_signal_metrics(self.metrics)
        self.assertAlmostEqual(out["current_month_discount"], 0)
        self.assertAlmostEqual(out["next_month_discount"], 0)
        self.assertAlmostEqual(out["roll_yield"], 0)
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
    """三因子预期收益模型：ROE=PB/PE、分红默认 1%、估值回归到 PE 10y 中位数。
    展期收益（roll_yield）由 status_line/期货合约表单独展示，不计入此 panel。"""

    def test_roe_derived_from_pb_pe(self):
        """PB/PE = E/B = ROE。PB=2.5, PE=35 → ROE ≈ 7.14%"""
        er = _compute_expected_return(
            close=8198, pe_ttm=35.0, pb=2.5, pe_median_10y=None)
        self.assertAlmostEqual(er["roe_pct"], 2.5 / 35.0 * 100, places=3)
        self.assertAlmostEqual(er["dividend_yield_pct"], 1.0)

    def test_compounding_3y_5y(self):
        """估值不变年化 = ROE + 分红；3年/5年复利按 (1+r)^n-1 算"""
        er = _compute_expected_return(
            close=8198, pe_ttm=35.0, pb=2.5, pe_median_10y=None)
        expected_base = 2.5 / 35.0 * 100 + 1.0
        self.assertAlmostEqual(er["annual_no_valuation_pct"], expected_base, places=2)
        expected_c3 = ((1 + expected_base / 100) ** 3 - 1) * 100
        expected_c5 = ((1 + expected_base / 100) ** 5 - 1) * 100
        self.assertAlmostEqual(er["c3y_no_valuation_pct"], expected_c3, places=2)
        self.assertAlmostEqual(er["c5y_no_valuation_pct"], expected_c5, places=2)

    def test_valuation_reversion_positive_when_undervalued(self):
        """PE 低于 10 年中位数 → 估值回归正值（有修复空间）"""
        er = _compute_expected_return(
            close=8198, pe_ttm=30.0, pb=2.4, pe_median_10y=40.0)
        self.assertAlmostEqual(er["valuation_change_pct"], (40 - 30) / 30 * 100,
                               places=2)
        self.assertGreater(er["valuation_change_pct"], 0)
        self.assertGreater(er["annual_with_mean_reversion_pct"],
                           er["annual_no_valuation_pct"])

    def test_valuation_reversion_negative_when_overvalued(self):
        """PE 高于 10 年中位数 → 估值回归负值（有回落风险）"""
        er = _compute_expected_return(
            close=8198, pe_ttm=45.0, pb=2.8, pe_median_10y=35.0)
        self.assertLess(er["valuation_change_pct"], 0)
        self.assertLess(er["annual_with_mean_reversion_pct"],
                        er["annual_no_valuation_pct"])

    def test_no_pe_median_zero_reversion(self):
        """无中位数（样本不足）→ 估值回归 0，含回归 == 估值不变"""
        er = _compute_expected_return(
            close=8198, pe_ttm=35.0, pb=2.5, pe_median_10y=None)
        self.assertEqual(er["valuation_change_pct"], 0.0)
        self.assertEqual(er["annual_with_mean_reversion_pct"],
                         er["annual_no_valuation_pct"])


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
