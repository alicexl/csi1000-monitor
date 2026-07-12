# tests/test_reporter.py
from __future__ import annotations
import unittest

from config import Config, Position, Thresholds
from signals import Signal
from reporter import generate_report, render_status_line, format_signals_section


def make_config(status="empty"):
    return Config(
        position=Position(status=status),
        thresholds=Thresholds(),
        pct_windows=["10y", "5y", "all"],
    )


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
        cfg = make_config("empty")
        metrics = make_metrics()
        sigs = [Signal("wait", 5, "PE 81.8%", {"pe": 81.8}, {"entry": 50}, "继续等待")]
        report = generate_report("2026-07-10", cfg, metrics, sigs)
        self.assertIn("2026-07-10", report)
        self.assertIn("空仓", report)
        self.assertIn("估值面板", report)
        self.assertIn("PE_TTM", report)
        self.assertIn("期货合约", report)
        self.assertIn("IM2607", report)

    def test_holding_state_report_shows_position(self):
        cfg = Config(
            position=Position(status="holding", contract="IM2607",
                              entry_date="2026-06-01", entry_price=7500.0),
            thresholds=Thresholds(),
        )
        metrics = make_metrics()
        sigs = [Signal("hold", 5, "持有", {}, {}, "继续持有")]
        report = generate_report("2026-07-10", cfg, metrics, sigs)
        self.assertIn("持仓", report)
        self.assertIn("IM2607", report)
        self.assertIn("7500", report)

    def test_report_contains_pe_pb_divergence(self):
        cfg = make_config()
        metrics = make_metrics()
        sigs = []
        report = generate_report("2026-07-10", cfg, metrics, sigs)
        self.assertIn("PE-PB 背离", report)
        self.assertIn("24.3", report)


class TestStatusLine(unittest.TestCase):
    def test_one_line_output(self):
        cfg = make_config("empty")
        metrics = make_metrics()
        line = render_status_line("2026-07-10", cfg, metrics, "wait")
        self.assertIn("2026-07-10", line)
        self.assertIn("空仓", line)
        self.assertIn("8198", line)
        self.assertIn("wait", line)


if __name__ == "__main__":
    unittest.main()
