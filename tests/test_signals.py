# tests/test_signals.py
from __future__ import annotations
import unittest

from signals import Signal, Thresholds, evaluate, score_carry, carry_suggestion

EMPTY = "empty"
HOLDING = "holding"


def make_metrics(pe_pct=50, discount=5, days=10, near_discount=None,
                 pb_pct=40, pbs_score=1.1):
    """构造 signals 输入。discount = 下月年化贴水；near_discount = 当月年化贴水。
    roll_yield = discount - near_discount（自动计算）。
    默认 near_discount = 3（比下月小，即正常 backwardation，roll_yield = discount - 3 > 0）。
    pb_pct/pbs_score 默认满足入场（PB 40<50, PBS 1.1>1.05），保证旧测试不回归。"""
    near = near_discount if near_discount is not None else 3
    return {
        "pe_ttm_pct_10y": pe_pct,
        "pb_pct_10y": pb_pct,
        "pbs_score": pbs_score,
        "current_month_discount": near,
        "current_month_days": days,
        "next_month_discount": discount,
        "roll_yield": discount - near,
    }


class TestEmptyState(unittest.TestCase):
    def setUp(self):
        self.t = Thresholds()

    def test_entry_signal(self):
        """PE<50 且 贴水>0 → entry"""
        sigs = evaluate(EMPTY, make_metrics(pe_pct=40, discount=8), self.t)
        types = [s.type for s in sigs]
        self.assertIn("entry", types)

    def test_entry_boundary_strict_lt_pe(self):
        """PE=50（严格 <50）不触发 entry"""
        sigs = evaluate(EMPTY, make_metrics(pe_pct=50, discount=8), self.t)
        types = [s.type for s in sigs]
        self.assertNotIn("entry", types)

    def test_entry_requires_pb_below_50(self):
        """新条件：PB 分位 ≥50% 不入场（即使 PE/贴水/PBS 达标）"""
        sigs = evaluate(EMPTY, make_metrics(
            pe_pct=40, discount=8, pb_pct=55), self.t)
        self.assertNotIn("entry", [s.type for s in sigs])

    def test_entry_requires_pbs_above_trend(self):
        """新条件：PBS_score ≤1.05 不入场（资产未低于趋势）"""
        sigs = evaluate(EMPTY, make_metrics(
            pe_pct=40, discount=8, pbs_score=1.0), self.t)
        self.assertNotIn("entry", [s.type for s in sigs])
        # PBS 1.1 >1.05 → 入场
        sigs2 = evaluate(EMPTY, make_metrics(
            pe_pct=40, discount=8, pbs_score=1.1), self.t)
        self.assertIn("entry", [s.type for s in sigs2])

    def test_entry_missing_pb_pbs_no_entry(self):
        """PB/PBS 缺失（数据不足）→ 保守不入场"""
        sigs = evaluate(EMPTY, make_metrics(
            pe_pct=40, discount=8, pb_pct=None, pbs_score=None), self.t)
        self.assertNotIn("entry", [s.type for s in sigs])

    def test_entry_boundary_strict_gt_discount(self):
        """贴水=0（严格 >0）不触发 entry"""
        sigs = evaluate(EMPTY, make_metrics(pe_pct=40, discount=0), self.t)
        types = [s.type for s in sigs]
        self.assertNotIn("entry", types)

    def test_entry_negative_discount_not_trigger(self):
        """贴水<0（升水）不触发 entry"""
        sigs = evaluate(EMPTY, make_metrics(pe_pct=40, discount=-1), self.t)
        types = [s.type for s in sigs]
        self.assertNotIn("entry", types)

    def test_warn_entry_pe_in_zone(self):
        """PE 在 50-60% 区间 → warn_entry"""
        sigs = evaluate(EMPTY, make_metrics(pe_pct=55, discount=8), self.t)
        types = [s.type for s in sigs]
        self.assertIn("warn_entry", types)

    def test_warn_entry_premium_state(self):
        """PE<50 但贴水<=0（升水状态）→ warn_entry（估值到但贴水失效）"""
        sigs = evaluate(EMPTY, make_metrics(pe_pct=40, discount=-1), self.t)
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
        self.assertIn("曲线健康", wait.condition)

    def test_wait_zone_high(self):
        """75-85% 偏高区文案"""
        sigs = evaluate(EMPTY, make_metrics(pe_pct=80, discount=8), self.t)
        wait = next(s for s in sigs if s.type == "wait")
        self.assertIn("偏高", wait.condition)
        self.assertIn("曲线健康", wait.condition)

    def test_wait_zone_excessive(self):
        """>=85% 过高区文案（空仓状态下不触发 reduce，但文案要体现）"""
        sigs = evaluate(EMPTY, make_metrics(pe_pct=90, discount=8), self.t)
        wait = next(s for s in sigs if s.type == "wait")
        self.assertIn("过高", wait.condition)

    def test_wait_roll_yield_tag(self):
        """wait 信号附带展期收益状态：曲线健康 vs 曲线异常"""
        sigs_hi = evaluate(EMPTY, make_metrics(pe_pct=70, discount=8), self.t)
        self.assertIn("曲线健康",
                      next(s for s in sigs_hi if s.type == "wait").condition)
        # near > far（倒挂）→ roll_yield < 0 → 曲线异常
        sigs_lo = evaluate(EMPTY, make_metrics(
            pe_pct=70, discount=2, near_discount=5), self.t)
        self.assertIn("曲线异常",
                      next(s for s in sigs_lo if s.type == "wait").condition)

    def test_warn_entry_pe_in_zone_with_discount(self):
        """warn_entry 接近入场分支 + roll_yield > 0 → 列出条件缺口"""
        sigs = evaluate(EMPTY, make_metrics(pe_pct=55, discount=8), self.t)
        we = next(s for s in sigs if s.type == "warn_entry")
        # PE 55% 未达 <50% → ✗PE；其余 ✓（PB40/PBS1.1/roll+5）
        self.assertIn("3/4", we.condition)
        self.assertIn("✗PE<50%", we.condition)
        self.assertIn("✓roll_yield", we.condition)

    def test_warn_entry_pe_in_zone_curve_flat(self):
        """warn_entry 接近入场分支 + 曲线扁平/倒挂（roll_yield≤0）"""
        sigs = evaluate(EMPTY, make_metrics(
            pe_pct=55, discount=3, near_discount=5), self.t)
        we = next(s for s in sigs if s.type == "warn_entry")
        # PE 55 ✗ + roll_yield -2 ✗；PB/PBS ✓ → 2/4
        self.assertIn("2/4", we.condition)
        self.assertIn("✗roll_yield", we.condition)

    def test_entry_when_curve_healthy_even_if_near_premium(self):
        """PE够低 + roll_yield > 0（即使近月升水，只要远月更深贴水）→ entry。

        旧逻辑（d_far > 0 AND d_near > 0）会要求近月也有贴水；
        新逻辑只看曲线斜率——近月异常升水但远月更深贴水时仍可入场（首次展期前能吃到）。
        """
        sigs = evaluate(EMPTY, make_metrics(
            pe_pct=40, discount=8, near_discount=-1), self.t)
        types = [s.type for s in sigs]
        self.assertIn("entry", types)
        self.assertNotIn("warn_entry", types)


class TestHoldingState(unittest.TestCase):
    def setUp(self):
        self.t = Thresholds()

    def test_reduce_pe_signal(self):
        """PE>85 → reduce（估值维度）"""
        sigs = evaluate(HOLDING, make_metrics(pe_pct=90, discount=8), self.t)
        types = [s.type for s in sigs]
        self.assertIn("reduce", types)

    def test_reduce_pe_boundary_strict_gt(self):
        """PE=85（严格 >85）不触发 reduce"""
        sigs = evaluate(HOLDING, make_metrics(pe_pct=85, discount=8), self.t)
        types = [s.type for s in sigs]
        self.assertNotIn("reduce", types)

    def test_reduce_basis_signal_zero(self):
        """贴水=0 → reduce（贴水维度，平水也算失效）"""
        sigs = evaluate(HOLDING, make_metrics(pe_pct=50, discount=0), self.t)
        types = [s.type for s in sigs]
        self.assertIn("reduce", types)

    def test_reduce_basis_signal_negative(self):
        """贴水<0（升水）→ reduce"""
        sigs = evaluate(HOLDING, make_metrics(pe_pct=50, discount=-2), self.t)
        types = [s.type for s in sigs]
        self.assertIn("reduce", types)

    def test_reduce_basis_not_trigger_when_curve_healthy(self):
        """roll_yield > 0（曲线向下倾斜）→ 不触发 reduce_basis"""
        sigs = evaluate(HOLDING, make_metrics(pe_pct=50, discount=8), self.t)
        reduce_sigs = [s for s in sigs if s.type == "reduce"]
        self.assertEqual(len(reduce_sigs), 0)

    def test_reduce_pe_and_basis_coexist(self):
        """PE>85 且 roll_yield<=0 → 两个 reduce 都触发"""
        sigs = evaluate(HOLDING, make_metrics(
            pe_pct=90, discount=3, near_discount=5), self.t)
        reduce_sigs = [s for s in sigs if s.type == "reduce"]
        self.assertEqual(len(reduce_sigs), 2)
        # 一个 condition 含 PE，一个含展期收益
        conds = " | ".join(s.condition for s in reduce_sigs)
        self.assertIn("PE_TTM", conds)
        self.assertIn("展期收益", conds)

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
        """PE<=75 且 天数>=7 且 贴水>0 → hold"""
        sigs = evaluate(HOLDING, make_metrics(pe_pct=50, discount=8, days=20), self.t)
        types = [s.type for s in sigs]
        self.assertIn("hold", types)

    def test_hold_not_trigger_on_premium(self):
        """升水状态（disc<=0）→ 不触发 hold（已被 reduce_basis 占据）"""
        sigs = evaluate(HOLDING, make_metrics(pe_pct=50, discount=0, days=20), self.t)
        types = [s.type for s in sigs]
        self.assertNotIn("hold", types)

    def test_reduce_pe_and_switch_can_coexist(self):
        """PE>85 且 天数<7 → reduce + switch 同时触发"""
        sigs = evaluate(HOLDING, make_metrics(pe_pct=90, discount=8, days=3), self.t)
        types = [s.type for s in sigs]
        self.assertIn("reduce", types)
        self.assertIn("switch", types)

    # ─── roll_yield 口径验证（2026-07-19 重构：策略判断基于曲线斜率）───
    def test_reduce_basis_when_curve_inverted(self):
        """曲线倒挂（near > far）→ reduce_basis 触发，即使两合约都有贴水"""
        sigs = evaluate(HOLDING, make_metrics(
            pe_pct=50, discount=3, days=20, near_discount=5), self.t)
        types = [s.type for s in sigs]
        self.assertIn("reduce", types)

    def test_no_reduce_when_curve_healthy(self):
        """曲线向下倾斜（far > near）→ 不触发 reduce_basis，即使当月升水"""
        sigs = evaluate(HOLDING, make_metrics(
            pe_pct=50, discount=8, days=20, near_discount=-1), self.t)
        reduce_sigs = [s for s in sigs if s.type == "reduce"]
        self.assertEqual(len(reduce_sigs), 0)

    def test_reduce_when_curve_flat(self):
        """曲线扁平（far == near）→ roll_yield = 0 → 触发 reduce_basis"""
        sigs = evaluate(HOLDING, make_metrics(
            pe_pct=50, discount=5, days=20, near_discount=5), self.t)
        types = [s.type for s in sigs]
        self.assertIn("reduce", types)


class TestPriority(unittest.TestCase):
    def setUp(self):
        self.t = Thresholds()

    def test_reduce_highest(self):
        sigs = evaluate(HOLDING, make_metrics(pe_pct=90, discount=8, days=3), self.t)
        # reduce 优先级最高（priority=1）
        top = min(sigs, key=lambda s: s.priority)
        self.assertEqual(top.type, "reduce")

    def test_reduce_basis_highest(self):
        """贴水变升水也是 priority=1"""
        sigs = evaluate(HOLDING, make_metrics(pe_pct=50, discount=-1, days=20), self.t)
        top = min(sigs, key=lambda s: s.priority)
        self.assertEqual(top.type, "reduce")
        self.assertEqual(top.priority, 1)

    def test_priority_order(self):
        """priority: reduce(1) > entry(2) > switch(3) > warn(4) > wait/hold(5)"""
        sigs = evaluate(HOLDING, make_metrics(pe_pct=90, discount=8, days=3), self.t)
        priorities = [s.priority for s in sigs]
        self.assertIn(1, priorities)  # reduce
        self.assertIn(3, priorities)  # switch


class TestConflictFiltering(unittest.TestCase):
    """evaluate() 后处理：wait/hold 与具体动作信号互斥。"""

    def setUp(self):
        self.t = Thresholds()

    def test_entry_filters_wait(self):
        """空仓 + entry 触发 → wait 被过滤掉"""
        sigs = evaluate(EMPTY, make_metrics(pe_pct=40, discount=8), self.t)
        types = [s.type for s in sigs]
        self.assertIn("entry", types)
        self.assertNotIn("wait", types)

    def test_warn_entry_filters_wait(self):
        """空仓 + warn_entry 触发 → wait 被过滤掉"""
        sigs = evaluate(EMPTY, make_metrics(pe_pct=55, discount=8), self.t)
        types = [s.type for s in sigs]
        self.assertIn("warn_entry", types)
        self.assertNotIn("wait", types)

    def test_wait_only_when_no_action(self):
        """空仓 + 无任何动作信号 → wait 兜底"""
        sigs = evaluate(EMPTY, make_metrics(pe_pct=70, discount=3), self.t)
        types = [s.type for s in sigs]
        self.assertEqual(types, ["wait"])

    def test_reduce_pe_filters_hold(self):
        """持仓 + reduce_pe 触发 → hold 被过滤掉"""
        sigs = evaluate(HOLDING, make_metrics(pe_pct=90, discount=8, days=20), self.t)
        types = [s.type for s in sigs]
        self.assertIn("reduce", types)
        self.assertNotIn("hold", types)

    def test_reduce_basis_filters_hold(self):
        """持仓 + reduce_basis 触发 → hold 被过滤掉"""
        sigs = evaluate(HOLDING, make_metrics(pe_pct=50, discount=-1, days=20), self.t)
        types = [s.type for s in sigs]
        self.assertIn("reduce", types)
        self.assertNotIn("hold", types)

    def test_switch_filters_hold(self):
        """持仓 + switch 触发 → hold 被过滤掉"""
        sigs = evaluate(HOLDING, make_metrics(pe_pct=50, discount=8, days=3), self.t)
        types = [s.type for s in sigs]
        self.assertIn("switch", types)
        self.assertNotIn("hold", types)

    def test_warn_reduce_filters_hold(self):
        """持仓 + warn_reduce 触发 → hold 被过滤掉"""
        sigs = evaluate(HOLDING, make_metrics(pe_pct=80, discount=8, days=20), self.t)
        types = [s.type for s in sigs]
        self.assertIn("warn_reduce", types)
        self.assertNotIn("hold", types)

    def test_hold_only_when_no_action(self):
        """持仓 + 无任何动作信号 → hold 兜底"""
        sigs = evaluate(HOLDING, make_metrics(pe_pct=50, discount=8, days=20), self.t)
        types = [s.type for s in sigs]
        self.assertEqual(types, ["hold"])


class TestCarryScore(unittest.TestCase):
    """IM Carry Score 三因子评分 + 档位建议。"""

    def setUp(self):
        self.t = Thresholds()

    def test_current_data_scores_60(self):
        """当前数据（贴水9.7/PB38.9/PBS0.99）→ 30+15+15=60，可持有档"""
        cs = score_carry(9.7, 38.9, 0.99, self.t)
        self.assertEqual(cs.total, 60)
        self.assertEqual(cs.discount_pts, 30)
        self.assertEqual(cs.pb_pts, 15)
        self.assertEqual(cs.pbs_pts, 15)
        self.assertEqual(cs.band, "holdable")

    def test_perfect_scores_100(self):
        """贴水≥10 + PB<30 + PBS>1.05 → 40+25+25=90（excellent 需≥80）"""
        cs = score_carry(12.0, 20.0, 1.20, self.t)
        self.assertEqual(cs.total, 90)
        self.assertEqual(cs.band, "excellent")

    def test_discount_tiers(self):
        """贴水分档：≥10→40, 5~10→30, <5→10"""
        self.assertEqual(score_carry(10.0, 20, 1.1, self.t).discount_pts, 40)
        self.assertEqual(score_carry(9.9, 20, 1.1, self.t).discount_pts, 30)
        self.assertEqual(score_carry(5.0, 20, 1.1, self.t).discount_pts, 30)
        self.assertEqual(score_carry(4.9, 20, 1.1, self.t).discount_pts, 10)

    def test_pb_tiers(self):
        """PB 分位分档：<30→25, 30~60→15, ≥60→5"""
        self.assertEqual(score_carry(10, 29.9, 1.1, self.t).pb_pts, 25)
        self.assertEqual(score_carry(10, 30.0, 1.1, self.t).pb_pts, 15)
        self.assertEqual(score_carry(10, 59.9, 1.1, self.t).pb_pts, 15)
        self.assertEqual(score_carry(10, 60.0, 1.1, self.t).pb_pts, 5)

    def test_pbs_tiers(self):
        """PBS_score 分档：>1.05→25, 0.85~1.05→15, <0.85→0"""
        self.assertEqual(score_carry(10, 40, 1.06, self.t).pbs_pts, 25)
        self.assertEqual(score_carry(10, 40, 1.05, self.t).pbs_pts, 15)
        self.assertEqual(score_carry(10, 40, 0.85, self.t).pbs_pts, 15)
        self.assertEqual(score_carry(10, 40, 0.84, self.t).pbs_pts, 0)

    def test_band_thresholds(self):
        """档位：≥80 excellent, 50~79 holdable, <50 wait"""
        self.assertEqual(score_carry(12, 20, 1.2, self.t).band, "excellent")
        # 40+15+15=70 → holdable
        self.assertEqual(score_carry(12, 40, 0.99, self.t).band, "holdable")
        # 10+5+0=15 → wait
        self.assertEqual(score_carry(3, 65, 0.80, self.t).band, "wait")

    def test_carry_suggestion_differs_by_state(self):
        """同档不同持仓状态建议不同"""
        # 可持有档
        self.assertIn("继续吃贴水", carry_suggestion("holdable", "holding"))
        self.assertIn("新增仓位等待", carry_suggestion("holdable", "empty"))
        # 极佳档
        self.assertIn("加仓", carry_suggestion("excellent", "holding"))
        self.assertIn("入场", carry_suggestion("excellent", "empty"))
        # 观望档
        self.assertIn("减仓", carry_suggestion("wait", "holding"))
        self.assertIn("不操作", carry_suggestion("wait", "empty"))


if __name__ == "__main__":
    unittest.main()
