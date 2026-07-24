# tests/test_signals.py
from __future__ import annotations
import unittest

from signals import Signal, Thresholds, evaluate

EMPTY = "empty"
HOLDING = "holding"


def make_metrics(pe_pct=50, roll_yield=1.0, days=10, pb_pct=40, roll_yield_q=1.0):
    """构造 signals 输入。roll_yield = 当月→下月展期段 = (当月价−下月价)/当月价（价格 back 判定）。
    roll_yield_q = 当月→下季展期段 = (当月价−下季价)/当月价（离场双段判定用）。
    默认两者 1.0 > 0（健康 backwardation）。
    pb_pct 默认满足入场（PB 40<50），保证旧测试不回归。
    current/next_month_discount 仅作展示占位，不参与信号判断。"""
    return {
        "pe_ttm_pct_10y": pe_pct,
        "pb_pct_10y": pb_pct,
        "current_month_discount": 5,   # 展示占位
        "current_month_days": days,
        "next_month_discount": 7,      # 展示占位
        "roll_yield": roll_yield,
        "roll_yield_q": roll_yield_q,
    }


class TestEmptyState(unittest.TestCase):
    def setUp(self):
        self.t = Thresholds()

    def test_entry_signal(self):
        """PE<50 且 价格 back → entry"""
        sigs = evaluate(EMPTY, make_metrics(pe_pct=40), self.t)
        types = [s.type for s in sigs]
        self.assertIn("entry", types)

    def test_entry_boundary_strict_lt_pe(self):
        """PE=50（严格 <50）不触发 entry"""
        sigs = evaluate(EMPTY, make_metrics(pe_pct=50), self.t)
        types = [s.type for s in sigs]
        self.assertNotIn("entry", types)

    def test_entry_requires_pb_below_50(self):
        """新条件：PB 分位 ≥50% 不入场（即使 PE/贴水/BPS 达标）"""
        sigs = evaluate(EMPTY, make_metrics(
            pe_pct=40, pb_pct=55), self.t)
        self.assertNotIn("entry", [s.type for s in sigs])

    def test_entry_missing_pb_no_entry(self):
        """PB 缺失（数据不足）→ 保守不入场"""
        sigs = evaluate(EMPTY, make_metrics(
            pe_pct=40, pb_pct=None), self.t)
        self.assertNotIn("entry", [s.type for s in sigs])

    def test_entry_boundary_strict_gt_roll_yield(self):
        """展期收益=0（平水，严格 >0）不触发 entry"""
        sigs = evaluate(EMPTY, make_metrics(pe_pct=40, roll_yield=0), self.t)
        types = [s.type for s in sigs]
        self.assertNotIn("entry", types)

    def test_entry_negative_roll_yield_not_trigger(self):
        """价格 contango（roll_yield<0）不触发 entry"""
        sigs = evaluate(EMPTY, make_metrics(pe_pct=40, roll_yield=-1.0), self.t)
        types = [s.type for s in sigs]
        self.assertNotIn("entry", types)

    def test_warn_entry_pe_in_zone(self):
        """PE 在 50-60% 区间 → warn_entry"""
        sigs = evaluate(EMPTY, make_metrics(pe_pct=55), self.t)
        types = [s.type for s in sigs]
        self.assertIn("warn_entry", types)

    def test_warn_entry_contango_state(self):
        """PE<50 但价格 contango（roll_yield≤0）→ warn_entry（估值到但展期失效）"""
        sigs = evaluate(EMPTY, make_metrics(pe_pct=40, roll_yield=-1.0), self.t)
        types = [s.type for s in sigs]
        self.assertIn("warn_entry", types)
        self.assertNotIn("entry", types)

    def test_wait_signal(self):
        """PE>=60 → wait"""
        sigs = evaluate(EMPTY, make_metrics(pe_pct=70), self.t)
        types = [s.type for s in sigs]
        self.assertIn("wait", types)

    def test_wait_zone_observation(self):
        """60-75% 观望区文案"""
        sigs = evaluate(EMPTY, make_metrics(pe_pct=70), self.t)
        wait = next(s for s in sigs if s.type == "wait")
        self.assertIn("观望区", wait.condition)
        self.assertIn("展期吃价差", wait.condition)

    def test_wait_zone_high(self):
        """75-85% 偏高区文案"""
        sigs = evaluate(EMPTY, make_metrics(pe_pct=80), self.t)
        wait = next(s for s in sigs if s.type == "wait")
        self.assertIn("偏高", wait.condition)
        self.assertIn("展期吃价差", wait.condition)

    def test_wait_zone_excessive(self):
        """>=85% 过高区文案（空仓状态下不触发 reduce，但文案要体现）"""
        sigs = evaluate(EMPTY, make_metrics(pe_pct=90), self.t)
        wait = next(s for s in sigs if s.type == "wait")
        self.assertIn("过高", wait.condition)

    def test_wait_roll_yield_tag(self):
        """wait 信号附带展期收益状态：价格 back（展期吃价差）vs contango/平水（展期失效）"""
        sigs_hi = evaluate(EMPTY, make_metrics(pe_pct=70), self.t)
        self.assertIn("展期吃价差",
                      next(s for s in sigs_hi if s.type == "wait").condition)
        # contango（roll_yield < 0）→ 展期失效
        sigs_lo = evaluate(EMPTY, make_metrics(pe_pct=70, roll_yield=-1.0), self.t)
        self.assertIn("展期失效",
                      next(s for s in sigs_lo if s.type == "wait").condition)

    def test_warn_entry_pe_in_zone_healthy_roll(self):
        """warn_entry 接近入场分支 + roll_yield > 0 → 列出条件缺口"""
        sigs = evaluate(EMPTY, make_metrics(pe_pct=55), self.t)
        we = next(s for s in sigs if s.type == "warn_entry")
        # PE 55% 未达 <50% → ✗PE；PB40/roll+1.0 ✓ → 2/3
        self.assertIn("2/3", we.condition)
        self.assertIn("✗PE<50%", we.condition)
        self.assertIn("✓roll_yield", we.condition)

    def test_warn_entry_pe_in_zone_contango(self):
        """warn_entry 接近入场分支 + 价格 contango（roll_yield≤0）"""
        sigs = evaluate(EMPTY, make_metrics(pe_pct=55, roll_yield=-1.0), self.t)
        we = next(s for s in sigs if s.type == "warn_entry")
        # PE 55 ✗ + roll_yield -1.0 ✗；PB40 ✓ → 1/3
        self.assertIn("1/3", we.condition)
        self.assertIn("✗roll_yield", we.condition)

    def test_entry_when_curve_back(self):
        """PE够低 + 价格 back（roll_yield > 0）→ entry。

        策略只看价格是否 backwardation（下月比当月便宜），不看当月相对现货贴水/升水——
        只要下月价 < 当月价，首次展期就能吃到价差。即使年化贴水斜率 ≤ 0，
        只要绝对价格 back 仍判健康（见 test_monitor.test_roll_yield_ignores_annualized_slope）。
        """
        sigs = evaluate(EMPTY, make_metrics(pe_pct=40), self.t)
        types = [s.type for s in sigs]
        self.assertIn("entry", types)
        self.assertNotIn("warn_entry", types)


class TestHoldingState(unittest.TestCase):
    def setUp(self):
        self.t = Thresholds()

    def test_reduce_pe_signal(self):
        """PE>85 → reduce（估值维度）"""
        sigs = evaluate(HOLDING, make_metrics(pe_pct=90), self.t)
        types = [s.type for s in sigs]
        self.assertIn("reduce", types)

    def test_reduce_pe_boundary_strict_gt(self):
        """PE=85（严格 >85）不触发 reduce"""
        sigs = evaluate(HOLDING, make_metrics(pe_pct=85), self.t)
        types = [s.type for s in sigs]
        self.assertNotIn("reduce", types)

    def test_reduce_basis_signal_zero(self):
        """平水（roll=0）单段，远月 back（roll_q>0）→ 未双段失效，不触发 reduce"""
        sigs = evaluate(HOLDING, make_metrics(pe_pct=50, roll_yield=0), self.t)
        types = [s.type for s in sigs]
        self.assertNotIn("reduce", types)

    def test_reduce_basis_signal_negative(self):
        """单段 contango（roll<0）但远月 back（roll_q>0）→ 未双段失效，不触发 reduce"""
        sigs = evaluate(HOLDING, make_metrics(pe_pct=50, roll_yield=-1.0), self.t)
        types = [s.type for s in sigs]
        self.assertNotIn("reduce", types)

    def test_reduce_basis_not_trigger_when_curve_healthy(self):
        """价格 back（roll_yield > 0）→ 不触发 reduce_basis"""
        sigs = evaluate(HOLDING, make_metrics(pe_pct=50), self.t)
        reduce_sigs = [s for s in sigs if s.type == "reduce"]
        self.assertEqual(len(reduce_sigs), 0)

    def test_reduce_pe_and_basis_coexist(self):
        """PE>85 且 双段 contango → reduce_pe + reduce_basis 都触发"""
        sigs = evaluate(HOLDING, make_metrics(
            pe_pct=90, roll_yield=-1.0, roll_yield_q=-1.0), self.t)
        reduce_sigs = [s for s in sigs if s.type == "reduce"]
        self.assertEqual(len(reduce_sigs), 2)
        # 一个 condition 含 PE，一个含展期双段失效
        conds = " | ".join(s.condition for s in reduce_sigs)
        self.assertIn("PE_TTM", conds)
        self.assertIn("展期双段失效", conds)

    def test_warn_reduce(self):
        """PE 在 75-85% → warn_reduce"""
        sigs = evaluate(HOLDING, make_metrics(pe_pct=80), self.t)
        types = [s.type for s in sigs]
        self.assertIn("warn_reduce", types)

    def test_switch_signal(self):
        """当月剩余天数 <7 → switch"""
        sigs = evaluate(HOLDING, make_metrics(pe_pct=50, days=5), self.t)
        types = [s.type for s in sigs]
        self.assertIn("switch", types)

    def test_switch_boundary_strict_lt(self):
        """剩余天数=7（严格 <7）不触发"""
        sigs = evaluate(HOLDING, make_metrics(pe_pct=50, days=7), self.t)
        types = [s.type for s in sigs]
        self.assertNotIn("switch", types)

    def test_hold_signal(self):
        """PE<=75 且 天数>=7 且 价格 back → hold"""
        sigs = evaluate(HOLDING, make_metrics(pe_pct=50, days=20), self.t)
        types = [s.type for s in sigs]
        self.assertIn("hold", types)

    def test_hold_on_flat_when_far_back(self):
        """平水（roll=0）但远月 back（roll_q>0）→ hold（中间态，未双段失效继续持有）"""
        sigs = evaluate(HOLDING, make_metrics(pe_pct=50, roll_yield=0, days=20), self.t)
        types = [s.type for s in sigs]
        self.assertIn("hold", types)
        self.assertNotIn("reduce", types)

    def test_hold_when_near_contango_far_back(self):
        """近月 contango（roll<0）但远月 back（roll_q>0）→ hold（中间态核心）"""
        sigs = evaluate(HOLDING, make_metrics(
            pe_pct=50, roll_yield=-1.0, roll_yield_q=1.0, days=20), self.t)
        types = [s.type for s in sigs]
        self.assertIn("hold", types)
        self.assertNotIn("reduce", types)

    def test_reduce_pe_and_switch_can_coexist(self):
        """PE>85 且 天数<7 → reduce + switch 同时触发"""
        sigs = evaluate(HOLDING, make_metrics(pe_pct=90, days=3), self.t)
        types = [s.type for s in sigs]
        self.assertIn("reduce", types)
        self.assertIn("switch", types)

    # ─── roll_yield 口径验证（2026-07-23 重构：策略判断基于价格是否 back）───
    def test_reduce_basis_when_double_contango(self):
        """双段 contango（roll<0 且 roll_q<0）→ reduce_basis 触发"""
        sigs = evaluate(HOLDING, make_metrics(
            pe_pct=50, roll_yield=-1.0, roll_yield_q=-1.0, days=20), self.t)
        self.assertIn("reduce", [s.type for s in sigs])

    def test_no_reduce_when_curve_back(self):
        """价格 back（roll_yield > 0）→ 不触发 reduce_basis"""
        sigs = evaluate(HOLDING, make_metrics(pe_pct=50, days=20), self.t)
        reduce_sigs = [s for s in sigs if s.type == "reduce"]
        self.assertEqual(len(reduce_sigs), 0)

    def test_no_reduce_when_flat_single_segment(self):
        """平水（roll=0）单段，远月 back → 不触发 reduce（未双段失效）"""
        sigs = evaluate(HOLDING, make_metrics(
            pe_pct=50, roll_yield=0, days=20), self.t)
        self.assertNotIn("reduce", [s.type for s in sigs])


class TestPriority(unittest.TestCase):
    def setUp(self):
        self.t = Thresholds()

    def test_reduce_highest(self):
        sigs = evaluate(HOLDING, make_metrics(pe_pct=90, days=3), self.t)
        # reduce 优先级最高（priority=1）
        top = min(sigs, key=lambda s: s.priority)
        self.assertEqual(top.type, "reduce")

    def test_reduce_basis_highest(self):
        """双段 contango（roll<0 且 roll_q<0）也是 priority=1"""
        sigs = evaluate(HOLDING, make_metrics(
            pe_pct=50, roll_yield=-1.0, roll_yield_q=-1.0, days=20), self.t)
        top = min(sigs, key=lambda s: s.priority)
        self.assertEqual(top.type, "reduce")
        self.assertEqual(top.priority, 1)

    def test_priority_order(self):
        """priority: reduce(1) > entry(2) > switch(3) > warn(4) > wait/hold(5)"""
        sigs = evaluate(HOLDING, make_metrics(pe_pct=90, days=3), self.t)
        priorities = [s.priority for s in sigs]
        self.assertIn(1, priorities)  # reduce
        self.assertIn(3, priorities)  # switch


class TestConflictFiltering(unittest.TestCase):
    """evaluate() 后处理：wait/hold 与具体动作信号互斥。"""

    def setUp(self):
        self.t = Thresholds()

    def test_entry_filters_wait(self):
        """空仓 + entry 触发 → wait 被过滤掉"""
        sigs = evaluate(EMPTY, make_metrics(pe_pct=40), self.t)
        types = [s.type for s in sigs]
        self.assertIn("entry", types)
        self.assertNotIn("wait", types)

    def test_warn_entry_filters_wait(self):
        """空仓 + warn_entry 触发 → wait 被过滤掉"""
        sigs = evaluate(EMPTY, make_metrics(pe_pct=55), self.t)
        types = [s.type for s in sigs]
        self.assertIn("warn_entry", types)
        self.assertNotIn("wait", types)

    def test_wait_only_when_no_action(self):
        """空仓 + 无任何动作信号 → wait 兜底"""
        sigs = evaluate(EMPTY, make_metrics(pe_pct=70), self.t)
        types = [s.type for s in sigs]
        self.assertEqual(types, ["wait"])

    def test_reduce_pe_filters_hold(self):
        """持仓 + reduce_pe 触发 → hold 被过滤掉"""
        sigs = evaluate(HOLDING, make_metrics(pe_pct=90, days=20), self.t)
        types = [s.type for s in sigs]
        self.assertIn("reduce", types)
        self.assertNotIn("hold", types)

    def test_reduce_basis_filters_hold(self):
        """持仓 + 双段 contango → reduce_basis 触发过滤 hold"""
        sigs = evaluate(HOLDING, make_metrics(
            pe_pct=50, roll_yield=-1.0, roll_yield_q=-1.0, days=20), self.t)
        types = [s.type for s in sigs]
        self.assertIn("reduce", types)
        self.assertNotIn("hold", types)

    def test_switch_filters_hold(self):
        """持仓 + switch 触发 → hold 被过滤掉"""
        sigs = evaluate(HOLDING, make_metrics(pe_pct=50, days=3), self.t)
        types = [s.type for s in sigs]
        self.assertIn("switch", types)
        self.assertNotIn("hold", types)

    def test_warn_reduce_filters_hold(self):
        """持仓 + warn_reduce 触发 → hold 被过滤掉"""
        sigs = evaluate(HOLDING, make_metrics(pe_pct=80, days=20), self.t)
        types = [s.type for s in sigs]
        self.assertIn("warn_reduce", types)
        self.assertNotIn("hold", types)

    def test_hold_only_when_no_action(self):
        """持仓 + 无任何动作信号 → hold 兜底"""
        sigs = evaluate(HOLDING, make_metrics(pe_pct=50, days=20), self.t)
        types = [s.type for s in sigs]
        self.assertEqual(types, ["hold"])


if __name__ == "__main__":
    unittest.main()
