# tests/test_bottom_trend.py
from __future__ import annotations
import unittest
from datetime import datetime

from monitor import (
    compute_bps, fit_bps_linear, fit_bps_log,
    pb_compression_scenarios, percentile_value,
    FIT_MIN_POINTS, PB_COMPRESSION_PERCENTILES,
)


def _ord(d: str) -> int:
    return datetime.strptime(d, "%Y-%m-%d").toordinal()


class TestComputeBps(unittest.TestCase):
    def test_basic(self):
        self.assertAlmostEqual(compute_bps(7000, 2.31), 7000 / 2.31)

    def test_zero_pb(self):
        self.assertEqual(compute_bps(7000, 0), 0.0)

    def test_negative_pb(self):
        self.assertEqual(compute_bps(7000, -1.0), 0.0)


class TestFitBpsLinear(unittest.TestCase):
    def test_perfect_linear_fit(self):
        """完美线性增长 → 斜率准确，R²=1.0"""
        # v = 100 + 105*t（t=年），首日 2014-01-01
        b = 105.0
        a = 100.0
        pts = []
        from datetime import date, timedelta
        first = date(2014, 1, 1)
        for y in range(0, 12):
            d = first + timedelta(days=365 * y)
            t = (d - first).days / 365.25
            pts.append((d.isoformat(), a + b * t))
        fit = fit_bps_linear(pts)
        self.assertIsNotNone(fit)
        self.assertAlmostEqual(fit["slope_pt_per_year"], b, places=1)
        self.assertAlmostEqual(fit["r2"], 1.0, places=6)
        self.assertEqual(fit["n"], 12)

    def test_too_few_points(self):
        """点数 <3 → None"""
        self.assertIsNone(fit_bps_linear([("2020-01-01", 100.0)] * 2))

    def test_nonpositive_skipped(self):
        """v<=0 跳过"""
        self.assertIsNone(fit_bps_linear([("2020-01-01", 100.0),
                                           ("2020-01-02", 0.0),
                                           ("2020-01-03", -1.0)]))


class TestPercentileValue(unittest.TestCase):
    def test_basic(self):
        # 1..100，50%分位：int(0.5*100)=50 → 索引50 = 51
        self.assertAlmostEqual(percentile_value(list(range(1, 101)), 50), 51)

    def test_low_pct(self):
        # 10%分位：int(0.1*100)=10 → 索引10 = 11
        self.assertAlmostEqual(percentile_value(list(range(1, 101)), 10), 11)

    def test_empty(self):
        self.assertIsNone(percentile_value([], 50))

    def test_clamps_high_pct(self):
        """pct 超过样本范围 → 取最大值（不越界）"""
        self.assertEqual(percentile_value([1, 2, 3], 99), 3)


class TestPbCompressionScenarios(unittest.TestCase):
    def test_uses_history_percentiles(self):
        """分位驱动：50%/15.9%/2.3% 分位对应历史 PB 值，σ 概率标签"""
        # 构造 PB 历史 1.0..3.0（100 个值），当前 PB=2.5
        pb_hist = [1.0 + i * 2.0 / 99 for i in range(100)]
        rows = pb_compression_scenarios(7260, 2.5, pb_hist, pcts=[50, 15.9, 2.3])
        self.assertIsNotNone(rows)
        # 当前行 + 3 个分位情景
        self.assertEqual(len(rows), 4)
        self.assertEqual(rows[0]["tag"], "当前")
        # 50% → 0σ 标签
        self.assertEqual(rows[1]["tag"], "PB 50%分位 (0σ)")
        # 50%分位 PB = 索引50 的值 = 1.0 + 50*2/99 ≈ 2.0101
        book = 7260 / 2.5
        self.assertAlmostEqual(rows[1]["price"], book * 2.0101, places=1)
        # 跌幅递增（分位越低 PB 越小 → 跌幅越大）
        drops = [r["drop_pct"] for r in rows[1:]]
        self.assertTrue(all(drops[i] > drops[i+1] for i in range(len(drops)-1)))
        self.assertTrue(all(d < 0 for d in drops))

    def test_sigma_label_mapping(self):
        """σ 概率标签：50→0σ, 15.9→-1σ, 2.3→-2σ；非映射分位无 σ 后缀"""
        pb_hist = [1.0 + i * 0.01 for i in range(100)]
        rows = pb_compression_scenarios(7260, 2.31, pb_hist,
                                        pcts=[50, 15.9, 2.3, 10])
        tags = [r["tag"] for r in rows[1:]]
        self.assertEqual(tags[0], "PB 50%分位 (0σ)")
        self.assertEqual(tags[1], "PB 15.9%分位 (-1σ)")
        self.assertEqual(tags[2], "PB 2.3%分位 (-2σ)")
        self.assertEqual(tags[3], "PB 10%分位")  # 10 不在 σ 映射 → 无后缀

    def test_drop_pct_formula(self):
        """drop_pct = (price - current) / current * 100"""
        pb_hist = [1.0, 2.0, 3.0]  # 50%分位 = 2.0
        rows = pb_compression_scenarios(7000, 2.0, pb_hist, pcts=[50])
        self.assertIsNotNone(rows)
        # PB 2.0 → price = book(3500)×2.0 = 7000 → 跌幅 0（当前也是2.0）
        # 改用 pcts=[10]：10%分位 = 1.0 → price 3500 → -50%
        rows = pb_compression_scenarios(7000, 2.0, pb_hist, pcts=[10])
        self.assertAlmostEqual(rows[1]["price"], 3500.0)
        self.assertAlmostEqual(rows[1]["drop_pct"], -50.0)

    def test_invalid_pb_returns_none(self):
        self.assertIsNone(pb_compression_scenarios(7260, 0))
        self.assertIsNone(pb_compression_scenarios(0, 2.31))

    def test_no_history_only_current_row(self):
        """无历史 PB → 退化为只有当前行"""
        rows = pb_compression_scenarios(7260, 2.31)
        self.assertIsNotNone(rows)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["tag"], "当前")

    def test_default_percentiles(self):
        """不传 pcts 用默认 PB_COMPRESSION_PERCENTILES（50/15.9/2.3）"""
        pb_hist = [1.0 + i * 0.01 for i in range(100)]
        rows = pb_compression_scenarios(7260, 2.31, pb_hist)
        self.assertEqual(len(rows), 1 + len(PB_COMPRESSION_PERCENTILES))


class TestFitBpsLog(unittest.TestCase):
    def test_perfect_exponential_fit(self):
        """全量点完美指数增长 → 对数回归 R²=1.0，年化增长率准确还原"""
        # ln(v) = a + b*t，取 b 使年化 10%：b = ln(1.10)/365
        import math
        b = math.log(1.10) / 365
        a = 5.0  # ln(v0)
        points = []
        for y in range(2015, 2026):
            d = f"{y}-06-15"
            v = math.exp(a + b * _ord(d))
            points.append((d, v))
        fit = fit_bps_log(points)
        self.assertIsNotNone(fit)
        self.assertAlmostEqual(fit["b"], b, places=8)
        self.assertAlmostEqual(fit["r2"], 1.0, places=6)
        # annual_pct = b*365*100 = ln(1.10)*100 ≈ 9.53%（连续复利年化口径）
        self.assertAlmostEqual(fit["annual_pct"], math.log(1.10) * 100, places=4)

    def test_too_few_points(self):
        """有效点 < FIT_MIN_POINTS → None"""
        points = [("2020-01-01", 100.0), ("2020-06-01", 90.0)]
        self.assertIsNone(fit_bps_log(points))

    def test_nonpositive_values_skipped(self):
        """v<=0 的点跳过（log 无定义）；剩太少返回 None"""
        points = [("2020-01-01", 100.0), ("2020-06-01", 0.0), ("2020-12-01", 90.0)]
        # 去掉 0 后只剩 2 个 < FIT_MIN_POINTS(3)
        self.assertIsNone(fit_bps_log(points))

    def test_no_window_field(self):
        """全量回归不带 window 字段（不再有低点检测窗口）"""
        import math
        points = [(f"202{y}-06-15", 100.0 * math.exp(0.03 * y)) for y in range(0, 8)]
        fit = fit_bps_log(points)
        self.assertNotIn("window", fit)


if __name__ == "__main__":
    unittest.main()
