# signals.py
from __future__ import annotations
from dataclasses import dataclass
from typing import Any


@dataclass
class Thresholds:
    entry_pe_pct: float = 50
    entry_pb_pct: float = 50  # 入场 PB 10y 分位门槛（资产端不贵）
    warn_entry_pe_pct: float = 60
    reduce_pe_pct: float = 85
    warn_reduce_pe_pct: float = 75
    switch_days: int = 7
    # 贴水阈值统一为 0（客观定义：>0 有贴水，<=0 升水），不作为可调参数
    # 展期收益阈值也统一为 0（roll_yield > 0 = 价格 backwardation，展期能吃到价差）

    # ─── IM Carry Score 阈值（滚贴水持有评分，满分 100）─────────────
    # 三因子：下季贴水年化(40) + PB 10y 分位(25) + 1年贴水覆盖-1σ(25)
    carry_discount_high: float = 10.0   # 下季贴水 ≥此值 → 40 分（极佳收益）
    carry_discount_low: float = 5.0     # 5~10% → 30 分；<此值 → 10 分
    carry_pb_low: float = 30.0           # PB 分位 <此值 → 25 分（资产便宜）
    carry_pb_high: float = 60.0          # 30~60% → 15 分；≥此值 → 5 分
    carry_coverage_full: float = 1.0     # 1年贴水/(-1σ跌幅) ≥此值 → 25 分（贴水够覆盖常态下跌）
    carry_coverage_half: float = 0.5     # 0.5~1.0 → 15 分；<此值 → 5 分（覆盖不足）
    carry_excellent: float = 80          # 总分 ≥此值 = 极佳开仓
    carry_holdable: float = 50           # 50~79 = 可持有；<此值 = 观望


@dataclass
class CarryScore:
    """IM Carry Score 评分结果。"""
    total: int                 # 总分 0~100
    discount_pts: int          # 下季贴水分
    pb_pts: int                # PB 分位分
    coverage_pts: int          # 1年贴水覆盖-1σ 分
    discount_value: float      # 下季贴水年化 %
    pb_pct: float             # PB 10y 分位 %
    coverage_ratio: float     # 1年贴水 / -1σ跌幅（≥1 = 已覆盖）
    band: str                 # "excellent" | "holdable" | "wait"


def score_carry(
    discount_pct: float, pb_pct: float, coverage_ratio: float, t: Thresholds,
) -> CarryScore:
    """计算 IM Carry Score（三因子加权，满分 100）。

    - discount_pct: 下季合约年化贴水（如 IM2612 的 9.7%），收益来源
    - pb_pct: PB 10 年分位，资产便宜度
    - coverage_ratio: 1 年贴水 / PB -1σ跌幅，≥1 表示贴水能覆盖一次常态杀跌

    滚贴水策略最怕：高估值 + 低贴水 + 波动上升。Carry Score 综合量化
    "收益（贴水）+ 安全（PB 估值 / 贴水覆盖）"，区分极佳开仓 / 可持有观望 / 观望。
    """
    # 贴水分（40）
    if discount_pct >= t.carry_discount_high:
        discount_pts = 40
    elif discount_pct >= t.carry_discount_low:
        discount_pts = 30
    else:
        discount_pts = 10
    # PB 分位分（25）
    if pb_pct < t.carry_pb_low:
        pb_pts = 25
    elif pb_pct < t.carry_pb_high:
        pb_pts = 15
    else:
        pb_pts = 5
    # 1年贴水覆盖-1σ 分（25）：贴水能否填平一次 PB 常态杀跌
    if coverage_ratio >= t.carry_coverage_full:
        coverage_pts = 25
    elif coverage_ratio >= t.carry_coverage_half:
        coverage_pts = 15
    else:
        coverage_pts = 5

    total = discount_pts + pb_pts + coverage_pts
    if total >= t.carry_excellent:
        band = "excellent"
    elif total >= t.carry_holdable:
        band = "holdable"
    else:
        band = "wait"
    return CarryScore(
        total=total, discount_pts=discount_pts, pb_pts=pb_pts,
        coverage_pts=coverage_pts, discount_value=discount_pct, pb_pct=pb_pct,
        coverage_ratio=coverage_ratio, band=band,
    )


def carry_suggestion(band: str, state: str) -> str:
    """Carry Score 档位 → 区分已持仓/空仓的具体建议。

    滚贴水≠价值投资：最怕高估值+低贴水+波动上升，而非单纯"贵一点"。
    因此 Carry 高分时已持仓继续吃贴水，空仓才考虑开仓。
    """
    if band == "excellent":
        if state == "holding":
            return "极佳持有区，继续吃贴水，可考虑加仓"
        return "极佳开仓点，贴水+估值双优，建议入场"
    if band == "holdable":
        if state == "holding":
            return "可持有继续吃贴水，无需操作"
        return "可持有区但非极佳开仓点，新增仓位等待更高贴水或估值回落"
    # wait
    if state == "holding":
        return "Carry 偏弱，关注贴水收敛与估值压力，考虑减仓"
    return "观望，贴水或估值不达标，不操作"


@dataclass
class Position:
    status: str = "empty"  # "empty" | "holding"
    contract: str | None = None
    entry_date: str | None = None
    entry_price: float | None = None


@dataclass
class Signal:
    type: str           # entry/warn_entry/reduce/warn_reduce/switch/wait/hold
    priority: int       # 1(最高) ~ 5
    condition: str
    current: dict
    threshold: dict
    suggestion: str


# ─── 空仓侧 ─────────────────────────────────────────────────────
# 策略判断用 roll_yield（展期收益 = 展期一次收益率 = (当月价 − 下月价)/当月价 = 价格是否 back）；
# roll_yield > 0 表示价格 backwardation（下月比当月便宜），展期（卖近月买远月）能吃到价差。
# 当月/下月年化贴水仅作展示参考（contracts 表 + status_line 附带）。
def _entry_signal(metrics: dict, t: Thresholds) -> Signal | None:
    """入场：估值低（PE+PB 双分位）AND 展期收益 > 0。

    三条件全满足才入场（"价格底" PE/PB 双分位 + 展期能吃到价差）：
    - PE_TTM 10y 分位 < entry_pe_pct（50%）：盈利端不贵
    - PB 10y 分位 < entry_pb_pct（50%）：资产端不贵
    - roll_yield > 0：价格 backwardation（下月比当月便宜），展期能吃到价差
    PB 缺失（数据不足）→ 保守不入场。任一不满足返回 None（fall through 到 warn_entry/wait）。
    """
    pe = metrics["pe_ttm_pct_10y"]
    pb = metrics.get("pb_pct_10y")
    roll = metrics["roll_yield"]
    if pb is None:
        return None
    if pe < t.entry_pe_pct and pb < t.entry_pb_pct and roll > 0:
        return Signal(
            type="entry", priority=2,
            condition=(f"PE_TTM 10y分位 {pe:.1f}% < {t.entry_pe_pct}% 且 "
                       f"PB 10y分位 {pb:.1f}% < {t.entry_pb_pct}% 且 "
                       f"展期收益 {roll:+.1f}% > 0"),
            current={"pe_ttm_pct_10y": pe, "pb_pct_10y": pb, "roll_yield": roll},
            threshold={"entry_pe_pct": t.entry_pe_pct,
                       "entry_pb_pct": t.entry_pb_pct},
            suggestion="买入 IM 当月合约入场（持有到交割后展期吃价差）",
        )
    return None


def _warn_entry_signal(metrics: dict, t: Thresholds) -> Signal | None:
    """预警入场：部分入场条件已满足但未全达标，列出到期条件提示缺口。

    入场三条件（见 _entry_signal）：PE<50% / PB<50% / roll_yield>0。
    若至少 PE 已进入 60% 以下区间（估值不算高），且其余条件有缺口 → 预警，
    列出每个条件的 ✓/✗，提示还需补什么。
    """
    pe = metrics["pe_ttm_pct_10y"]
    roll = metrics["roll_yield"]
    pb = metrics.get("pb_pct_10y")

    # PE 仍偏高（≥warn_entry 60%）→ 不预警，交给 wait
    if pe >= t.warn_entry_pe_pct:
        return None

    # PE 已 <60%，逐条检查三条件，收集缺口
    checks = [
        ("PE<50%", pe < t.entry_pe_pct, f"PE {pe:.1f}%"),
        ("PB<50%", pb is not None and pb < t.entry_pb_pct,
         f"PB {pb:.1f}%" if pb is not None else "PB N/A"),
        ("roll_yield>0", roll > 0, f"展期 {roll:+.1f}%"),
    ]
    met = [name for name, ok, _ in checks if ok]
    missing = [name for name, ok, _ in checks if not ok]
    # 全满足 → 已是 entry，不预警；全不满足且 PE 仍 >50% → 交给 wait 处理
    if not missing:
        return None

    # 至少一个非 PE 条件接近（PB 满足，或 PE 已 <50%）才预警，避免噪音
    other_met = any(ok for name, ok, _ in checks if name != "PE<50%")
    if pe >= t.entry_pe_pct and not other_met:
        return None

    detail = "  ".join(f"{'✓' if ok else '✗'}{lbl}({val})"
                       for lbl, ok, val in checks)
    cond = (f"接近入场区：{len(met)}/3 条件满足 — {detail}")
    return Signal(
        type="warn_entry", priority=4,
        condition=cond,
        current={"pe_ttm_pct_10y": pe, "pb_pct_10y": pb, "roll_yield": roll},
        threshold={"warn_entry_pe_pct": t.warn_entry_pe_pct,
                   "entry_pe_pct": t.entry_pe_pct,
                   "entry_pb_pct": t.entry_pb_pct},
        suggestion=f"密切跟踪，待补齐：{', '.join(missing)}",
    )


def _wait_signal(metrics: dict, t: Thresholds) -> Signal:
    pe = metrics["pe_ttm_pct_10y"]
    roll = metrics["roll_yield"]
    if pe >= t.reduce_pe_pct:
        zone = f"过高（≥{t.reduce_pe_pct}%），等待估值回落"
    elif pe >= t.warn_reduce_pe_pct:
        zone = f"偏高（{t.warn_reduce_pe_pct}-{t.reduce_pe_pct}%），不宜入场"
    else:
        zone = f"观望区（{t.warn_entry_pe_pct}-{t.warn_reduce_pe_pct}%）"
    roll_tag = (f"展期收益 {roll:+.1f}% > 0（价格 back，展期吃价差）"
                if roll > 0
                else f"展期收益 {roll:+.1f}% ≤ 0（价格 contango/平水，展期失效）")
    return Signal(
        type="wait", priority=5,
        condition=f"PE_TTM 分位 {pe:.1f}% {zone}；{roll_tag}",
        current={"pe_ttm_pct_10y": pe, "roll_yield": roll},
        threshold={"warn_entry_pe_pct": t.warn_entry_pe_pct,
                   "warn_reduce_pe_pct": t.warn_reduce_pe_pct,
                   "reduce_pe_pct": t.reduce_pe_pct},
        suggestion="继续等待，不需要操作",
    )


# ─── 持仓侧 ─────────────────────────────────────────────────────
def _reduce_pe_signal(metrics: dict, t: Thresholds) -> Signal | None:
    """退出条件 1：估值过高。"""
    pe = metrics["pe_ttm_pct_10y"]
    if pe > t.reduce_pe_pct:
        return Signal(
            type="reduce", priority=1,
            condition=f"PE_TTM 10y分位 {pe:.1f}% > {t.reduce_pe_pct}%",
            current={"pe_ttm_pct_10y": pe},
            threshold={"reduce_pe_pct": t.reduce_pe_pct},
            suggestion="减仓/平仓止盈",
        )
    return None


def _reduce_basis_signal(metrics: dict, t: Thresholds) -> Signal | None:
    """退出条件 2：展期收益 ≤ 0（价格 contango 或平水）。

    roll_yield = (当月价 − 下月价)/当月价 = 展期一次收益率。
    ≤ 0 表示下月不再比当月便宜（价格 contango/平水），展期吃贴水策略前提失效。
    """
    roll = metrics["roll_yield"]
    if roll <= 0:
        return Signal(
            type="reduce", priority=1,
            condition=(f"展期收益 {roll:+.1f}% ≤ 0%（价格 contango/平水），"
                       f"展期吃贴水策略前提失效"),
            current={"roll_yield": roll},
            threshold={"exit_roll_yield": 0},
            suggestion="平仓——价格 contango 状态下展期会反向亏钱",
        )
    return None


def _warn_reduce_signal(metrics: dict, t: Thresholds) -> Signal | None:
    pe = metrics["pe_ttm_pct_10y"]
    if t.warn_reduce_pe_pct < pe <= t.reduce_pe_pct:
        return Signal(
            type="warn_reduce", priority=4,
            condition=f"PE_TTM 分位 {pe:.1f}% 在 {t.warn_reduce_pe_pct}-{t.reduce_pe_pct}% 区间",
            current={"pe_ttm_pct_10y": pe},
            threshold={"warn_reduce_pe_pct": t.warn_reduce_pe_pct, "reduce_pe_pct": t.reduce_pe_pct},
            suggestion="准备减仓",
        )
    return None


def _switch_signal(metrics: dict, t: Thresholds) -> Signal | None:
    days = metrics["current_month_days"]
    if days < t.switch_days:
        return Signal(
            type="switch", priority=3,
            condition=f"当月合约剩余 {days} 天 < {t.switch_days} 天",
            current={"days_to_expire": days},
            threshold={"switch_days": t.switch_days},
            suggestion="考虑平当月、开下月",
        )
    return None


def _hold_signal(metrics: dict, t: Thresholds) -> Signal | None:
    pe = metrics["pe_ttm_pct_10y"]
    days = metrics["current_month_days"]
    roll = metrics["roll_yield"]
    if pe <= t.warn_reduce_pe_pct and days >= t.switch_days and roll > 0:
        return Signal(
            type="hold", priority=5,
            condition=(f"PE_TTM {pe:.1f}% ≤ {t.warn_reduce_pe_pct}% 且 "
                       f"剩余 {days} 天 ≥ {t.switch_days} 且 "
                       f"展期收益 {roll:+.1f}% > 0"),
            current={"pe_ttm_pct_10y": pe, "days_to_expire": days,
                     "roll_yield": roll},
            threshold={"warn_reduce_pe_pct": t.warn_reduce_pe_pct, "switch_days": t.switch_days},
            suggestion="继续持有吃贴水",
        )
    return None


def evaluate(
    state: str, metrics: dict[str, Any], thresholds: Thresholds
) -> list[Signal]:
    """根据持仓状态 + 指标 + 阈值 → 返回信号列表（已按 priority 排序）。

    后处理过滤：wait/hold 是兜底信号，与 entry/warn_entry/reduce/warn_reduce/switch
    互斥——有具体动作信号时就不显示"继续等待/继续持有"，避免语义冲突。
    """
    sigs: list[Signal] = []

    if state == "empty":
        for fn in (_entry_signal, _warn_entry_signal, _wait_signal):
            s = fn(metrics, thresholds)
            if s is not None:
                sigs.append(s)
        # 有动作信号时过滤 wait
        if any(s.type in ("entry", "warn_entry") for s in sigs):
            sigs = [s for s in sigs if s.type != "wait"]
    elif state == "holding":
        for fn in (_reduce_pe_signal, _reduce_basis_signal,
                   _warn_reduce_signal, _switch_signal, _hold_signal):
            s = fn(metrics, thresholds)
            if s is not None:
                sigs.append(s)
        # 有动作信号时过滤 hold
        if any(s.type in ("reduce", "warn_reduce", "switch") for s in sigs):
            sigs = [s for s in sigs if s.type != "hold"]
    else:
        sigs.append(Signal(
            type="wait", priority=5,
            condition=f"未知持仓状态: {state}",
            current={}, threshold={},
            suggestion="检查 monitor.py 的 POSITION.status",
        ))

    sigs.sort(key=lambda s: s.priority)
    return sigs
