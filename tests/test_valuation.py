# tests/test_valuation.py
from __future__ import annotations
import unittest

from valuation import percentile, compute_pct_for_windows, pe_pb_divergence


class TestPercentile(unittest.TestCase):
    def test_basic(self):
        series = [10, 20, 30, 40, 50]
        self.assertAlmostEqual(percentile(series, 30), 60.0)

    def test_below_all(self):
        self.assertAlmostEqual(percentile([10, 20, 30], 5), 0.0)

    def test_above_all(self):
        self.assertAlmostEqual(percentile([10, 20, 30], 40), 100.0)

    def test_equal_to_min(self):
        self.assertAlmostEqual(percentile([10, 20, 30], 10), 33.33, places=1)

    def test_empty_series(self):
        self.assertAlmostEqual(percentile([], 30), 0.0)


class TestComputePctForWindows(unittest.TestCase):
    def test_multi_window_all_within_5y(self):
        """10 个点都在近 2 年内 -> 5y/10y/all 窗口都包含全部"""
        history = []
        for i in range(10):
            # 2025-09 到 2026-06，都在近 5 年内
            months = [("2025", 9), ("2025", 10), ("2025", 11), ("2025", 12),
                      ("2026", 1), ("2026", 2), ("2026", 3), ("2026", 4),
                      ("2026", 5), ("2026", 6)]
            y, m = months[i]
            history.append({"date": f"{y}-{m:02d}-01", "pe_ttm": 20 + i * 2})
        # pe_ttm: 20,22,24,26,28,30,32,34,36,38
        current = {"pe_ttm": 30}
        result = compute_pct_for_windows(
            history, current, "pe_ttm", ["10y", "5y", "all"])
        # 所有窗口都包含全部 10 个点: <=30 的有 6 个 -> 60%
        self.assertAlmostEqual(result["all"], 60.0)
        self.assertAlmostEqual(result["5y"], 60.0)
        self.assertAlmostEqual(result["10y"], 60.0)

    def test_5y_window_excludes_old_data(self):
        """5 个老点(2020) + 5 个新点(2026) -> 5y 只含新点"""
        history = []
        # 5 个老点 (2020, 5y 窗口会排除)
        for i in range(5):
            history.append({"date": f"2020-0{i+1}-01", "pe_ttm": 10 + i * 2})
        # 5 个新点 (2026, 5y 窗口包含)
        for i in range(5):
            history.append({"date": f"2026-0{i+1}-01", "pe_ttm": 30 + i * 2})
        # 全历史 pe_ttm: 10,12,14,16,18,30,32,34,36,38
        # 5y 窗口 pe_ttm: 30,32,34,36,38 (只有后5个)
        current = {"pe_ttm": 30}
        result = compute_pct_for_windows(
            history, current, "pe_ttm", ["5y", "all"])
        # all: <=30 有 6 个(10,12,14,16,18,30) -> 60%
        self.assertAlmostEqual(result["all"], 60.0)
        # 5y: <=30 有 1 个(30) -> 20%
        self.assertAlmostEqual(result["5y"], 20.0)

    def test_missing_field_returns_zeros(self):
        result = compute_pct_for_windows(
            [{"date": "2026-01-01"}], {"pe_ttm": 30}, "pe_ttm", ["all"])
        self.assertEqual(result, {"all": 0.0})


class TestPePbDivergence(unittest.TestCase):
    def test_positive(self):
        self.assertAlmostEqual(pe_pb_divergence(70, 50), 20.0)

    def test_negative(self):
        self.assertAlmostEqual(pe_pb_divergence(40, 60), -20.0)

    def test_zero(self):
        self.assertAlmostEqual(pe_pb_divergence(50, 50), 0.0)


if __name__ == "__main__":
    unittest.main()
