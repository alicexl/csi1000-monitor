# tests/test_valuation.py
from __future__ import annotations
import unittest

from monitor import percentile, compute_pct_for_windows, pe_pb_divergence, MIN_SAMPLES


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
    def _build_history(self, dates_values):
        return [{"date": d, "pe_ttm": v} for d, v in dates_values]

    def test_multi_window_all_within_5y(self):
        """10 个点都在近 2 年内 -> 5y/10y/all 窗口都包含全部"""
        months = [("2025", 9), ("2025", 10), ("2025", 11), ("2025", 12),
                  ("2026", 1), ("2026", 2), ("2026", 3), ("2026", 4),
                  ("2026", 5), ("2026", 6)]
        history = [{"date": f"{y}-{m:02d}-01", "pe_ttm": 20 + i * 2}
                   for i, (y, m) in enumerate(months)]
        current = {"pe_ttm": 30}
        result = compute_pct_for_windows(
            history, current, "pe_ttm", ["10y", "5y", "all"])
        # 样本数 < MIN_SAMPLES(100) → pct=None
        for w in ("10y", "5y", "all"):
            self.assertIsNone(result[w]["pct"])
            self.assertEqual(result[w]["n"], 10)

    def test_5y_window_excludes_old_data(self):
        """5 个老点(2020) + 5 个新点(2026) -> 5y 只含新点；样本不足 pct=None"""
        history = []
        for i in range(5):
            history.append({"date": f"2020-0{i+1}-01", "pe_ttm": 10 + i * 2})
        for i in range(5):
            history.append({"date": f"2026-0{i+1}-01", "pe_ttm": 30 + i * 2})
        current = {"pe_ttm": 30}
        result = compute_pct_for_windows(
            history, current, "pe_ttm", ["5y", "all"])
        # 样本不足，pct 都是 None
        self.assertIsNone(result["5y"]["pct"])
        self.assertEqual(result["5y"]["n"], 5)
        self.assertIsNone(result["all"]["pct"])
        self.assertEqual(result["all"]["n"], 10)

    def test_enough_samples_computes_pct(self):
        """样本数 >= MIN_SAMPLES 时正常算分位"""
        # 生成 150 个点（>= 100），值 1..150
        history = [{"date": f"2024-{(i % 12) + 1:02d}-15", "pe_ttm": i + 1}
                   for i in range(150)]
        current = {"pe_ttm": 75}  # 中位数附近
        result = compute_pct_for_windows(
            history, current, "pe_ttm", ["10y", "all"])
        # all 窗口样本 150 个，<=75 的有 75 个 → 50%
        self.assertAlmostEqual(result["all"]["pct"], 50.0)
        self.assertEqual(result["all"]["n"], 150)

    def test_missing_field_returns_none_pct(self):
        """current 缺字段 → 所有窗口 pct=None, n=0"""
        result = compute_pct_for_windows(
            [{"date": "2026-01-01"}], {"pe_ttm": 30}, "pe_ttm", ["all"])
        self.assertEqual(result["all"], {"pct": None, "n": 0})


class TestPePbDivergence(unittest.TestCase):
    def test_positive(self):
        self.assertAlmostEqual(pe_pb_divergence(70, 50), 20.0)

    def test_negative(self):
        self.assertAlmostEqual(pe_pb_divergence(40, 60), -20.0)

    def test_zero(self):
        self.assertAlmostEqual(pe_pb_divergence(50, 50), 0.0)


if __name__ == "__main__":
    unittest.main()
