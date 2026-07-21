# tests/test_bottom_trend.py
from __future__ import annotations
import unittest
from datetime import datetime

from monitor import (
    compute_pbs, detect_local_lows, fit_bottom_trend,
    pb_compression_scenarios, percentile_value, _compute_theory_points,
    BOTTOM_WINDOW_DAYS, BOTTOM_MIN_POINTS, PB_COMPRESSION_PERCENTILES,
    THEORY_DEV_PERCENTILES,
)


def _ord(d: str) -> int:
    return datetime.strptime(d, "%Y-%m-%d").toordinal()


class TestComputePbs(unittest.TestCase):
    def test_basic(self):
        self.assertAlmostEqual(compute_pbs(7000, 2.31), 7000 / 2.31)

    def test_zero_pb(self):
        self.assertEqual(compute_pbs(7000, 0), 0.0)

    def test_negative_pb(self):
        self.assertEqual(compute_pbs(7000, -1.0), 0.0)


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
        """分位驱动：50%/25%/10% 分位对应历史 PB 值"""
        # 构造 PB 历史 1.0..3.0（100 个值），当前 PB=2.5
        pb_hist = [1.0 + i * 2.0 / 99 for i in range(100)]
        rows = pb_compression_scenarios(7260, 2.5, pb_hist, pcts=[50, 25, 10])
        self.assertIsNotNone(rows)
        # 当前行 + 3 个分位情景
        self.assertEqual(len(rows), 4)
        self.assertEqual(rows[0]["tag"], "当前")
        self.assertEqual(rows[1]["tag"], "PB 50%分位")
        # 50%分位 PB = 索引50 的值 = 1.0 + 50*2/99 ≈ 2.0101
        book = 7260 / 2.5
        self.assertAlmostEqual(rows[1]["price"], book * 2.0101, places=1)
        # 跌幅递增（分位越低 PB 越小 → 跌幅越大）
        drops = [r["drop_pct"] for r in rows[1:]]
        self.assertTrue(all(drops[i] > drops[i+1] for i in range(len(drops)-1)))
        self.assertTrue(all(d < 0 for d in drops))

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
        """不传 pcts 用默认 PB_COMPRESSION_PERCENTILES（50/25/10）"""
        pb_hist = [1.0 + i * 0.01 for i in range(100)]
        rows = pb_compression_scenarios(7260, 2.31, pb_hist)
        self.assertEqual(len(rows), 1 + len(PB_COMPRESSION_PERCENTILES))


class TestComputeTheoryPoints(unittest.TestCase):
    """回归框架理论点位：PBS 偏离比分位 × 趋势线 × 当前PB。"""

    def test_structure_and_trend_line_row(self):
        """首行是回归趋势线（偏离比=1.0），末行是当前"""
        # 偏离比 0.8~1.2 均匀分布
        ratios = [0.8 + i * 0.4 / 99 for i in range(100)]
        rows = _compute_theory_points(ratios, trend_now=3110,
                                      current_pb=2.31, current_pbs=3143)
        self.assertIsNotNone(rows)
        self.assertEqual(rows[0]["tag"], "回归趋势线")
        self.assertAlmostEqual(rows[0]["ratio"], 1.0)
        self.assertAlmostEqual(rows[0]["price"], 3110 * 2.31)
        self.assertEqual(rows[-1]["tag"], "当前")
        # 当前行 price = current_pbs × pb = 3143 × 2.31
        self.assertAlmostEqual(rows[-1]["price"], 3143 * 2.31)

    def test_lower_percentile_lower_price(self):
        """分位越低 → 偏离比越小 → 理论点位越低"""
        ratios = [0.8 + i * 0.4 / 99 for i in range(100)]
        rows = _compute_theory_points(ratios, 3110, 2.31, 3143)
        # 趋势线行 + 3 分位行 + 当前行
        self.assertEqual(len(rows), 1 + len(THEORY_DEV_PERCENTILES) + 1)
        # 50% > 25% > 10% 分位对应点位递减
        prices = [r["price"] for r in rows[1:4]]
        self.assertTrue(prices[0] > prices[1] > prices[2])

    def test_drop_pct_relative_to_trend_line(self):
        """距趋势线 = (price - 趋势线点位) / 趋势线点位 × 100"""
        ratios = [1.0] * 100  # 全部偏离=1，分位都是1.0
        rows = _compute_theory_points(ratios, 3110, 2.31, 3110)
        # 趋势线行 drop=0；分位行偏离=1 → drop=0；当前=pbs3110 → drop=0
        for r in rows:
            self.assertAlmostEqual(r["drop_pct"], 0.0, places=2)

    def test_empty_ratios_returns_none(self):
        self.assertIsNone(_compute_theory_points([], 3110, 2.31, 3143))
        self.assertIsNone(_compute_theory_points([1.0], 0, 2.31, 3143))
        self.assertIsNone(_compute_theory_points([1.0], 3110, 0, 3143))


class TestDetectLocalLows(unittest.TestCase):
    def test_finds_valleys(self):
        # V 形：10/20/10，两端各留 window 个点避免边界
        n = BOTTOM_WINDOW_DAYS
        flat = 100.0
        pts = [(f"2020-01-{i+1:02d}", flat) for i in range(n)]
        pts.append(("2020-04-01", 50.0))  # 低点
        pts += [(f"2020-04-{i+2:02d}", flat) for i in range(n)]
        pts.append(("2020-07-01", 50.0))  # 第二个低点（同值）
        pts += [(f"2020-07-{i+2:02d}", flat) for i in range(n)]
        pts.sort()
        lows = detect_local_lows(pts, n)
        # 两个 50.0 都是各自 ±n 窗口的最小值
        self.assertEqual(len(lows), 2)
        self.assertEqual(lows[0][1], 50.0)

    def test_too_few_points(self):
        """点数 < 2*window+1 → 返回空"""
        pts = [("2020-01-01", float(i)) for i in range(BOTTOM_WINDOW_DAYS)]
        self.assertEqual(detect_local_lows(pts, BOTTOM_WINDOW_DAYS), [])

    def test_plateau_picks_one(self):
        """连续多日同一最低值：窗口内首个最低日被选中（之后被新窗口再选，去重靠调用方）"""
        n = BOTTOM_WINDOW_DAYS
        pts = [(f"2020-01-{i+1:02d}", 100.0) for i in range(n)]
        # 一段 50.0 的平台
        for i in range(3):
            pts.append((f"2020-04-0{i+1}", 50.0))
        pts += [(f"2020-04-{i+4:02d}", 100.0) for i in range(n)]
        pts.sort()
        lows = detect_local_lows(pts, n)
        # 平台内每个 50.0 在自己的 ±n 窗口里都是最小 → 都被选中
        self.assertTrue(all(v == 50.0 for _, v in lows))
        self.assertGreater(len(lows), 0)


class TestFitBottomTrend(unittest.TestCase):
    def test_perfect_exponential_fit(self):
        """完美指数增长低点 → R²=1.0，年化抬升准确还原"""
        # ln(v) = a + b*t，取 b 使年化 10%：b = ln(1.10)/365
        import math
        b = math.log(1.10) / 365
        a = 5.0  # ln(v0)
        lows = []
        for y in range(2015, 2026):
            d = f"{y}-06-15"
            v = math.exp(a + b * _ord(d))
            lows.append((d, v))
        fit = fit_bottom_trend(lows)
        self.assertIsNotNone(fit)
        self.assertAlmostEqual(fit["b"], b, places=8)
        self.assertAlmostEqual(fit["r2"], 1.0, places=6)
        # annual_pct = b*365*100 = ln(1.10)*100 ≈ 9.53%（连续复利年化口径）
        self.assertAlmostEqual(fit["annual_pct"], math.log(1.10) * 100, places=4)

    def test_too_few_lows(self):
        """低点数 < BOTTOM_MIN_POINTS → None"""
        lows = [("2020-01-01", 100.0), ("2020-06-01", 90.0)]
        self.assertIsNone(fit_bottom_trend(lows))

    def test_nonpositive_values_skipped(self):
        """v<=0 的低点跳过（log 无定义）；剩太少返回 None"""
        lows = [("2020-01-01", 100.0), ("2020-06-01", 0.0), ("2020-12-01", 90.0)]
        # 去掉 0 后只剩 2 个 < BOTTOM_MIN_POINTS(3)
        self.assertIsNone(fit_bottom_trend(lows))

    def test_returns_window_field(self):
        """fit 带 window 字段供 reporter 展示"""
        import math
        lows = [(f"202{y}-06-15", 100.0 * math.exp(0.03 * y)) for y in range(0, 8)]
        fit = fit_bottom_trend(lows, window=42)
        self.assertEqual(fit["window"], 42)


class TestComputeBottomTrendIntegration(unittest.TestCase):
    """_compute_bottom_trend 的集成层：用真实 DB 数据验证 pbs_score =
    trend_now / current_pbs、理论点位结构。E2E 数值比合成数据更能暴露
    问题（合成 sin 波形难以复现真实市场的结构性底部）。"""

    def _real_history(self):
        """读真实 daily_valuation 表（2014-10 后）。无 DB 时 skip。"""
        import sqlite3
        from pathlib import Path
        db = Path(__file__).parent.parent / "csi1000_monitor.db"
        if not db.exists():
            self.skipTest("csi1000_monitor.db 不存在")
        conn = sqlite3.connect(str(db))
        conn.row_factory = sqlite3.Row
        rows = [dict(r) for r in conn.execute(
            "SELECT date, close, pb FROM daily_valuation "
            "WHERE pb>0 AND close>0 AND date>='2014-10-17' ORDER BY date"
        ).fetchall()]
        conn.close()
        return rows

    def test_pbs_score_is_trend_over_current(self):
        """pbs_score = trend_now / current_pbs（fair/now）"""
        from monitor import _compute_bottom_trend
        rows = self._real_history()
        last = rows[-1]
        bt = _compute_bottom_trend(rows, last["close"], last["pb"])
        self.assertIsNotNone(bt, "真实数据应能拟合底部趋势")
        expected = bt["trend_now"] / bt["current_pbs"]
        self.assertAlmostEqual(bt["pbs_score"], expected, places=4)

    def test_theory_points_structure(self):
        """理论点位含回归趋势线 + 3 个分位 + 当前行，价格随分位递减"""
        from monitor import _compute_bottom_trend
        rows = self._real_history()
        last = rows[-1]
        bt = _compute_bottom_trend(rows, last["close"], last["pb"])
        self.assertIsNotNone(bt)
        tp = bt["theory_points"]
        self.assertIsNotNone(tp)
        self.assertEqual(tp[0]["tag"], "回归趋势线")
        self.assertEqual(tp[-1]["tag"], "当前")
        # 50% > 25% > 10% 分位对应价格递减
        prices = [tp[1]["price"], tp[2]["price"], tp[3]["price"]]
        self.assertTrue(prices[0] > prices[1] > prices[2])

    def test_score_near_one_current(self):
        """当前 PBS_score 应落在合理区间（0.8-1.2），真实数据长期贴近趋势线"""
        from monitor import _compute_bottom_trend
        rows = self._real_history()
        last = rows[-1]
        bt = _compute_bottom_trend(rows, last["close"], last["pb"])
        self.assertIsNotNone(bt)
        self.assertGreater(bt["pbs_score"], 0.8)
        self.assertLess(bt["pbs_score"], 1.2)


if __name__ == "__main__":
    unittest.main()
