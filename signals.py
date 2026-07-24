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
            suggestion="平仓止盈",
        )
    return None


def _reduce_basis_signal(metrics: dict, t: Thresholds) -> Signal | None:
    """退出条件 2：展期双段同时 contango（当月→下月 <0 且 当月→下季 <0）。

    roll_yield = (当月价 − 下月价)/当月价（月度展期段），
    roll_yield_q = (当月价 − 下季价)/当月价（当月→下季展期段）。
    两段均 <0 表示当月比下月、下季都便宜（双段 contango），期限结构彻底反转，
    展期吃贴水策略前提失效。单段 contango 可能只是交割噪音，不离场。
    """
    roll = metrics["roll_yield"]
    roll_q = metrics["roll_yield_q"]
    if roll < 0 and roll_q < 0:
        return Signal(
            type="reduce", priority=1,
            condition=(f"展期双段失效：当月→下月 {roll:+.1f}% 且 "
                       f"当月→下季 {roll_q:+.1f}% 均<0（双段 contango），"
                       f"展期吃贴水策略前提失效"),
            current={"roll_yield": roll, "roll_yield_q": roll_q},
            threshold={"exit_roll_yield": 0},
            suggestion="平仓——双段 contango 状态下展期会反向亏钱",
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
            suggestion="准备平仓",
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
    roll_q = metrics["roll_yield_q"]
    basis_failed = roll < 0 and roll_q < 0  # 双段 contango = 离场条件，hold 的否定
    if (pe <= t.warn_reduce_pe_pct and days >= t.switch_days
            and not basis_failed):
        return Signal(
            type="hold", priority=5,
            condition=(f"PE_TTM {pe:.1f}% ≤ {t.warn_reduce_pe_pct}% 且 "
                       f"剩余 {days} 天 ≥ {t.switch_days} 且 "
                       f"展期未双段失效（月{roll:+.1f}%/季{roll_q:+.1f}%）"),
            current={"pe_ttm_pct_10y": pe, "days_to_expire": days,
                     "roll_yield": roll, "roll_yield_q": roll_q},
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
