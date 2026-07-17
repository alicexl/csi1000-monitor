# tests/test_reporter.py
from __future__ import annotations
import unittest

from signals import Signal, Position, Thresholds
from reporter import generate_report, render_status_line, format_signals_section


def make_position(status="empty"):
    return Position(status=status)


def make_metrics():
    return {
        "date": "2026-07-10",
        "close": 8198.31,
        "pe_ttm": 34.57,
        "pe_static": 35.77,
        "pb": 2.58,
        "pe_ttm_pct": {"10y": 81.8, "5y": 94.1, "all": 69.6},
        "pe_static_pct": {"10y": 75.9, "5y": 86.5, "all": 64.6},
        "pb_pct": {"10y": 57.5, "5y": 73.5, "all": 48.9},
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
        "main_continuous_discount_pct": 65.0,
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
        line = render_status_line("2026-07-10", pos, metrics, "wait", 30.5)
        self.assertIn("2026-07-10", line)
        self.assertIn("空仓", line)
        self.assertIn("8198", line)
        self.assertIn("wait", line)
        self.assertIn("30.5", line)

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


class TestOperationAdvice(unittest.TestCase):
    """操作建议：同 priority 多信号都展示。"""

    def test_single_advice(self):
        pos = make_position("empty")
        metrics = make_metrics()
        sigs = [Signal("wait", 5, "PE 70%", {}, {}, "继续等待")]
        report = generate_report("2026-07-10", pos, metrics, sigs)
        self.assertIn("继续等待", report)

    def test_multiple_top_priority_advices(self):
        """两个 priority=1 的 reduce 信号，suggestion 都应出现在操作建议里"""
        pos = make_position("holding")
        metrics = make_metrics()
        sigs = [
            Signal("reduce", 1, "PE > 85%", {"pe": 90}, {}, "估值过高，平仓止盈"),
            Signal("reduce", 1, "贴水 ≤ 0", {"disc": -1}, {}, "升水失效，立即平仓"),
            Signal("switch", 3, "天数 <7", {}, {}, "考虑换月"),
        ]
        report = generate_report("2026-07-10", pos, metrics, sigs)
        # 操作建议 section 只提取出来看
        advice_section = report.split("## 操作建议")[1]
        self.assertIn("估值过高，平仓止盈", advice_section)
        self.assertIn("升水失效，立即平仓", advice_section)
        # switch 不是 top priority，不出现在操作建议 section
        self.assertNotIn("考虑换月", advice_section)


if __name__ == "__main__":
    unittest.main()
