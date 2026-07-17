# tests/test_signals.py
from __future__ import annotations
import unittest

from config import Thresholds
from signals import Signal, evaluate

EMPTY = "empty"
HOLDING = "holding"


def make_metrics(pe_pct=50, discount=5, days=10):
    return {
        "pe_ttm_pct_10y": pe_pct,
        "current_month_discount": discount,
        "current_month_days": days,
    }


class TestEmptyState(unittest.TestCase):
    def setUp(self):
        self.t = Thresholds()

    def test_entry_signal(self):
        """PE<50 且 贴水>5 → entry"""
        sigs = evaluate(EMPTY, make_metrics(pe_pct=40, discount=8), self.t)
        types = [s.type for s in sigs]
        self.assertIn("entry", types)

    def test_entry_boundary_strict_lt(self):
        """PE=50（严格 <50）不触发 entry"""
        sigs = evaluate(EMPTY, make_metrics(pe_pct=50, discount=8), self.t)
        types = [s.type for s in sigs]
        self.assertNotIn("entry", types)

    def test_entry_boundary_strict_gt_discount(self):
        """贴水=5（严格 >5）不触发 entry"""
        sigs = evaluate(EMPTY, make_metrics(pe_pct=40, discount=5), self.t)
        types = [s.type for s in sigs]
        self.assertNotIn("entry", types)

    def test_warn_entry_pe_in_zone(self):
        """PE 在 50-60% 区间 → warn_entry"""
        sigs = evaluate(EMPTY, make_metrics(pe_pct=55, discount=8), self.t)
        types = [s.type for s in sigs]
        self.assertIn("warn_entry", types)

    def test_warn_entry_discount_low(self):
        """PE<50 但贴水<=5 → warn_entry（估值到但贴水不够）"""
        sigs = evaluate(EMPTY, make_metrics(pe_pct=40, discount=3), self.t)
        types = [s.type for s in sigs]
        self.assertIn("warn_entry", types)
        self.assertNotIn("entry", types)

    def test_wait_signal(self):
        """PE>=60 → wait"""
        sigs = evaluate(EMPTY, make_metrics(pe_pct=70, discount=3), self.t)
        types = [s.type for s in sigs]
        self.assertIn("wait", types)

    def test_wait_zone_observation(self):
        """60-75% 观望区文案"""
        sigs = evaluate(EMPTY, make_metrics(pe_pct=70, discount=8), self.t)
        wait = next(s for s in sigs if s.type == "wait")
        self.assertIn("观望区", wait.condition)
        self.assertIn("已达标", wait.condition)

    def test_wait_zone_high(self):
        """75-85% 偏高区文案"""
        sigs = evaluate(EMPTY, make_metrics(pe_pct=80, discount=3), self.t)
        wait = next(s for s in sigs if s.type == "wait")
        self.assertIn("偏高", wait.condition)
        self.assertIn("不足", wait.condition)

    def test_wait_zone_excessive(self):
        """>=85% 过高区文案（空仓状态下不触发 reduce，但文案要体现）"""
        sigs = evaluate(EMPTY, make_metrics(pe_pct=90, discount=8), self.t)
        wait = next(s for s in sigs if s.type == "wait")
        self.assertIn("过高", wait.condition)

    def test_wait_discount_tag(self):
        """wait 信号附带贴水状态：贴水足 vs 不足"""
        sigs_hi = evaluate(EMPTY, make_metrics(pe_pct=70, discount=8), self.t)
        self.assertIn("已达标", next(s for s in sigs_hi if s.type == "wait").condition)
        sigs_lo = evaluate(EMPTY, make_metrics(pe_pct=70, discount=3), self.t)
        self.assertIn("不足", next(s for s in sigs_lo if s.type == "wait").condition)

    def test_warn_entry_pe_in_zone_with_discount(self):
        """warn_entry 接近入场分支带贴水状态"""
        sigs = evaluate(EMPTY, make_metrics(pe_pct=55, discount=8), self.t)
        we = next(s for s in sigs if s.type == "warn_entry")
        self.assertIn("已达标", we.condition)


class TestHoldingState(unittest.TestCase):
    def setUp(self):
        self.t = Thresholds()

    def test_reduce_signal(self):
        """PE>85 → reduce"""
        sigs = evaluate(HOLDING, make_metrics(pe_pct=90, discount=8), self.t)
        types = [s.type for s in sigs]
        self.assertIn("reduce", types)

    def test_reduce_boundary_strict_gt(self):
        """PE=85（严格 >85）不触发 reduce"""
        sigs = evaluate(HOLDING, make_metrics(pe_pct=85, discount=8), self.t)
        types = [s.type for s in sigs]
        self.assertNotIn("reduce", types)

    def test_warn_reduce(self):
        """PE 在 75-85% → warn_reduce"""
        sigs = evaluate(HOLDING, make_metrics(pe_pct=80, discount=8), self.t)
        types = [s.type for s in sigs]
        self.assertIn("warn_reduce", types)

    def test_switch_signal(self):
        """当月剩余天数 <7 → switch"""
        sigs = evaluate(HOLDING, make_metrics(pe_pct=50, discount=8, days=5), self.t)
        types = [s.type for s in sigs]
        self.assertIn("switch", types)

    def test_switch_boundary_strict_lt(self):
        """剩余天数=7（严格 <7）不触发"""
        sigs = evaluate(HOLDING, make_metrics(pe_pct=50, discount=8, days=7), self.t)
        types = [s.type for s in sigs]
        self.assertNotIn("switch", types)

    def test_hold_signal(self):
        """PE<=75 且 天数>=7 → hold"""
        sigs = evaluate(HOLDING, make_metrics(pe_pct=50, discount=8, days=20), self.t)
        types = [s.type for s in sigs]
        self.assertIn("hold", types)

    def test_reduce_and_switch_can_coexist(self):
        """PE>85 且 天数<7 → reduce + switch 同时触发"""
        sigs = evaluate(HOLDING, make_metrics(pe_pct=90, discount=8, days=3), self.t)
        types = [s.type for s in sigs]
        self.assertIn("reduce", types)
        self.assertIn("switch", types)


class TestPriority(unittest.TestCase):
    def setUp(self):
        self.t = Thresholds()

    def test_reduce_highest(self):
        sigs = evaluate(HOLDING, make_metrics(pe_pct=90, discount=8, days=3), self.t)
        # reduce 优先级最高（priority=1）
        top = min(sigs, key=lambda s: s.priority)
        self.assertEqual(top.type, "reduce")

    def test_priority_order(self):
        """priority: reduce(1) > entry(2) > switch(3) > warn(4) > wait/hold(5)"""
        sigs = evaluate(HOLDING, make_metrics(pe_pct=90, discount=8, days=3), self.t)
        # 至少 reduce(priority=1) 和 switch(priority=3)
        priorities = [s.priority for s in sigs]
        self.assertIn(1, priorities)  # reduce
        self.assertIn(3, priorities)  # switch


if __name__ == "__main__":
    unittest.main()
