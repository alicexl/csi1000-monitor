# tests/test_reporter.py
from __future__ import annotations
import unittest

from signals import Signal, Position, Thresholds
from reporter import (generate_report, render_status_line, format_signals_section,
                      _fmt_pct_window, _expected_return_panel, _discount_coverage_panel,
                      _entry_check_panel, _exit_check_panel)


def make_position(status="empty"):
    return Position(status=status)


def _pct_entry(pct, n=2440):
    return {"pct": pct, "n": n}


def make_metrics():
    return {
        "date": "2026-07-10",
        "close": 8198.31,
        "pe_ttm": 34.57,
        "pb": 2.58,
        "pe_ttm_pct": {"10y": _pct_entry(81.8), "5y": _pct_entry(94.1, 1220),
                       "all": _pct_entry(69.6, 2900)},
        "pb_pct": {"10y": _pct_entry(57.5), "5y": _pct_entry(73.5, 1220),
                   "all": _pct_entry(48.9, 2900)},
        "eps_ttm": 237.15,
        "bps": 3177.64,
        "pe_pb_divergence": 24.3,
        "contracts": [
            {"symbol": "IM2607", "contract_type": "当月", "close": 8150,
             "days_to_expire": 7, "expire_date": "2026-07-17",
             "basis": -48.31, "annualized_discount": 30.5},
            {"symbol": "IM2608", "contract_type": "下月", "close": 8098,
             "days_to_expire": 35, "expire_date": "2026-08-21",
             "basis": -100.31, "annualized_discount": 12.8},
        ],
        "expected_return": {
            "roe_pct": 2.58 / 34.57 * 100,  # ≈ 7.46
            "dividend_yield_pct": 1.0,
            "pe_median_10y": 32.0,
            "valuation_change_pct": (32.0 - 34.57) / 34.57 * 100,  # ≈ -7.43
            "annual_no_valuation_pct": 2.58 / 34.57 * 100 + 1.0,
            "c3y_no_valuation_pct": ((1 + (2.58 / 34.57 * 100 + 1.0) / 100) ** 3 - 1) * 100,
            "c5y_no_valuation_pct": ((1 + (2.58 / 34.57 * 100 + 1.0) / 100) ** 5 - 1) * 100,
            "annual_with_mean_reversion_pct": (2.58 / 34.57 * 100 + 1.0
                                                + (32.0 - 34.57) / 34.57 * 100),
        },
    }


class TestFormatSignals(unittest.TestCase):
    def test_empty_state_wait(self):
        sigs = [Signal("wait", 5, "PE高", {}, {}, "继续等待")]
        out = format_signals_section(sigs, "empty")
        self.assertIn("wait", out)
        self.assertIn("继续等待", out)


class TestGenerateReport(unittest.TestCase):
    def test_empty_state_report_contains_sections(self):
        pos = make_position("empty")
        metrics = make_metrics()
        sigs = [Signal("wait", 5, "PE 81.8%", {"pe": 81.8}, {"entry": 50}, "继续等待")]
        report = generate_report("2026-07-10", pos, metrics, sigs)
        self.assertIn("2026-07-10", report)
        self.assertIn("空仓", report)
        self.assertIn("估值面板", report)
        self.assertIn("PE_TTM", report)
        self.assertIn("期货合约", report)
        self.assertIn("IM2607", report)

    def test_holding_state_report_shows_position(self):
        pos = Position(status="holding", contract="IM2607",
                       entry_date="2026-06-01", entry_price=7500.0)
        metrics = make_metrics()
        sigs = [Signal("hold", 5, "持有", {}, {}, "继续持有")]
        report = generate_report("2026-07-10", pos, metrics, sigs)
        self.assertIn("持仓", report)
        self.assertIn("IM2607", report)
        self.assertIn("7500", report)

    def test_report_contains_pe_pb_divergence(self):
        pos = make_position()
        metrics = make_metrics()
        sigs = []
        report = generate_report("2026-07-10", pos, metrics, sigs)
        self.assertIn("PE-PB 背离", report)
        self.assertIn("24.3", report)


class TestStatusLine(unittest.TestCase):
    def test_one_line_output(self):
        pos = make_position("empty")
        metrics = make_metrics()
        line = render_status_line("2026-07-10", pos, metrics, "wait", 2.5)
        self.assertIn("2026-07-10", line)
        self.assertIn("空仓", line)
        self.assertIn("8198", line)
        self.assertIn("wait", line)
        self.assertIn("展期收益", line)

    def test_status_line_uses_roll_yield_label(self):
        """status_line 应显示'展期收益'标签（roll_yield = 展期一次收益率，价格是否 back）"""
        pos = make_position("empty")
        metrics = make_metrics()
        line = render_status_line("2026-07-10", pos, metrics, "wait", 2.5)
        self.assertIn("展期收益", line)
        self.assertNotIn("下月贴水", line)

    def test_emoji_from_signal_type(self):
        """emoji 直接从 signal_type 映射，不再依赖 pe_pct 阈值"""
        pos = make_position("holding")
        metrics = make_metrics()
        # reduce → ⚠
        line = render_status_line("2026-07-10", pos, metrics, "reduce", -0.5)
        self.assertIn("⚠", line)
        # switch → 🔄
        line = render_status_line("2026-07-10", pos, metrics, "switch", 5.0)
        self.assertIn("🔄", line)
        # hold → ✅
        line = render_status_line("2026-07-10", pos, metrics, "hold", 5.0)
        self.assertIn("✅", line)
        # entry → 🟢
        pos_empty = make_position("empty")
        line = render_status_line("2026-07-10", pos_empty, metrics, "entry", 5.0)
        self.assertIn("🟢", line)
        # wait → 空 emoji
        line = render_status_line("2026-07-10", pos_empty, metrics, "wait", 5.0)
        self.assertNotIn("⚠", line)
        self.assertNotIn("🟢", line)


class TestExpectedReturnPanel(unittest.TestCase):
    """三因子预期收益 panel 渲染（ROE + 分红 + 估值变动，无展期收益分量）。"""

    def test_panel_contains_factors(self):
        er = make_metrics()["expected_return"]
        out = _expected_return_panel(er)
        self.assertIn("ROE", out)
        self.assertIn("分红率", out)
        self.assertIn("估值回归", out)
        # 展期收益不再作为单独分量（看 status_line 和期货合约表）
        self.assertNotIn("展期收益（下月年化贴水）", out)

    def test_panel_shows_compounding(self):
        er = make_metrics()["expected_return"]
        out = _expected_return_panel(er)
        self.assertIn("估值不变年化预期", out)
        self.assertIn("3 年复利", out)
        self.assertIn("5 年复利", out)

    def test_panel_in_full_report(self):
        pos = make_position("holding")
        metrics = make_metrics()
        sigs = [Signal("hold", 5, "持有", {}, {}, "继续持有")]
        report = generate_report("2026-07-10", pos, metrics, sigs)
        self.assertIn("预期收益", report)
        self.assertIn("ROE", report)


class TestDiscountCoveragePanel(unittest.TestCase):
    """贴水覆盖性 panel：持有 1 年的展期贴水 vs PB 杀跌，标已覆盖/未覆盖。"""

    def _cov(self):
        return {
            "discount_annual": 15.0,
            "years": [1],
            "scenarios": [
                {"label": "-1σ", "drop_pct": -11.0},
                {"label": "-2σ", "drop_pct": -26.2},
            ],
        }

    def test_coverage_labels(self):
        # 1年-1σ: 15-11=+4 已覆盖；1年-2σ: 15-26.2=-11.2 未覆盖
        out = _discount_coverage_panel(self._cov())
        self.assertIn("✅ 已覆盖", out)
        self.assertIn("❌ 未覆盖", out)
        self.assertIn("累计贴水", out)
        self.assertIn("跌11%", out)

    def test_margin_values(self):
        out = _discount_coverage_panel(self._cov())
        self.assertIn("+15.0%", out)   # 1 年累计贴水
        self.assertIn("+4.0%", out)    # 1 年 -1σ 已覆盖 margin

    def test_in_full_report(self):
        metrics = make_metrics()
        metrics["discount_coverage"] = self._cov()
        report = generate_report("2026-07-10", make_position(), metrics, [])
        self.assertIn("贴水覆盖性", report)


class TestEntryCheckPanel(unittest.TestCase):
    """开仓信号检查：PE/PB 分位区间 + 展期收益 三条件达标与否。"""

    def _m(self, pe_pct, pb_pct, roll):
        return {
            "pe_ttm_pct": {"10y": {"pct": pe_pct, "n": 2400}},
            "pb_pct": {"10y": {"pct": pb_pct, "n": 2400}},
            "roll_yield": roll,
        }

    def test_all_met_fits_entry(self):
        """PE/PB<50% + roll>0 → 符合开仓信号，区间标'入场区'"""
        out = _entry_check_panel(self._m(40, 40, 2.0))
        self.assertIn("符合开仓信号", out)
        self.assertIn("入场区", out)

    def test_pe_high_blocks_entry(self):
        """PE 72%（观望区）→ 未达开仓"""
        out = _entry_check_panel(self._m(72, 35, 2.0))
        self.assertIn("未达开仓", out)
        self.assertIn("观望区", out)

    def test_roll_negative_blocks_entry(self):
        """展期 contango（roll<0）→ 未达开仓"""
        out = _entry_check_panel(self._m(40, 40, -1.0))
        self.assertIn("未达开仓", out)

    def test_in_full_report_empty_state_only(self):
        """空仓报告含开仓信号检查；持仓状态不展示"""
        metrics = make_metrics()
        metrics["roll_yield"] = 2.0
        report = generate_report("2026-07-10", make_position("empty"), metrics, [])
        self.assertIn("开仓信号检查", report)
        report_holding = generate_report(
            "2026-07-10", make_position("holding"), metrics, [])
        self.assertNotIn("开仓信号检查", report_holding)


class TestExitCheckPanel(unittest.TestCase):
    """平仓信号检查：PE>85% 或 roll_yield≤0，任一触发即平仓。"""

    def _m(self, pe_pct, roll):
        return {
            "pe_ttm_pct": {"10y": {"pct": pe_pct, "n": 2400}},
            "roll_yield": roll,
        }

    def test_safe_no_exit(self):
        """PE 72%（安全区）+ roll>0 → 未触发平仓，继续持有"""
        out = _exit_check_panel(self._m(72, 2.0))
        self.assertIn("未触发平仓", out)
        self.assertIn("安全区", out)

    def test_pe_high_triggers_exit(self):
        """PE 90%（平仓区）→ 触发平仓（PE 过高）"""
        out = _exit_check_panel(self._m(90, 2.0))
        self.assertIn("触发平仓信号", out)
        self.assertIn("PE 过高", out)
        self.assertIn("平仓区", out)

    def test_roll_bad_triggers_exit(self):
        """展期 contango（roll≤0）→ 触发平仓（展期失效）"""
        out = _exit_check_panel(self._m(72, -1.0))
        self.assertIn("触发平仓信号", out)
        self.assertIn("展期失效", out)

    def test_in_full_report_holding_state_only(self):
        """持仓报告含平仓信号检查；空仓不展示"""
        metrics = make_metrics()
        metrics["roll_yield"] = 2.0
        report = generate_report("2026-07-10", make_position("holding"), metrics, [])
        self.assertIn("平仓信号检查", report)
        report_empty = generate_report(
            "2026-07-10", make_position("empty"), metrics, [])
        self.assertNotIn("平仓信号检查", report_empty)


class TestFmtPctWindow(unittest.TestCase):

    def test_normal(self):
        """正常: '72.3% (n=2427)'，不显示 /expected"""
        entry = {"pct": 72.3, "n": 2427}
        out = _fmt_pct_window(entry)
        self.assertEqual(out, "72.3% (n=2427)")
        self.assertNotIn("⚠", out)
        self.assertNotIn("/", out)

    def test_absolute_low_samples_returns_na(self):
        """pct=None（n < MIN_SAMPLES）→ 'N/A ⚠ (n=50)'"""
        entry = {"pct": None, "n": 50}
        out = _fmt_pct_window(entry)
        self.assertEqual(out, "N/A ⚠ (n=50)")

    def test_none_entry(self):
        """entry=None → 'N/A'"""
        self.assertEqual(_fmt_pct_window(None), "N/A")


if __name__ == "__main__":
    unittest.main()
