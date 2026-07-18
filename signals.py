# signals.py
from __future__ import annotations
from dataclasses import dataclass
from typing import Any


@dataclass
class Thresholds:
    entry_pe_pct: float = 50
    warn_entry_pe_pct: float = 60
    reduce_pe_pct: float = 85
    warn_reduce_pe_pct: float = 75
    switch_days: int = 7
    # 贴水阈值统一为 0（客观定义：>0 有贴水，<=0 升水），不作为可调参数
    # 展期收益阈值也统一为 0（roll_yield > 0 = 曲线向下倾斜，展期能吃到价差）


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
# 策略判断用 roll_yield（展期收益 = 下月年化贴水 - 当月年化贴水 = 期限结构斜率）；
# roll_yield > 0 表示曲线向下倾斜（远月更深贴水），展期能吃到价差。
# 当月/下月绝对贴水仅作展示参考（contracts 表 + status_line 附带）。
def _entry_signal(metrics: dict, t: Thresholds) -> Signal | None:
    """入场：估值低 AND 展期收益 > 0（曲线向下倾斜，展期能吃到价差）。"""
    pe = metrics["pe_ttm_pct_10y"]
    roll = metrics["roll_yield"]
    if pe < t.entry_pe_pct and roll > 0:
        return Signal(
            type="entry", priority=2,
            condition=(f"PE_TTM 10y分位 {pe:.1f}% < {t.entry_pe_pct}% "
                       f"且 展期收益 {roll:+.1f}% > 0（曲线向下倾斜，展期可吃）"),
            current={"pe_ttm_pct_10y": pe, "roll_yield": roll},
            threshold={"entry_pe_pct": t.entry_pe_pct},
            suggestion="买入 IM 当月合约入场（持有到交割后展期吃价差）",
        )
    return None


def _warn_entry_signal(metrics: dict, t: Thresholds) -> Signal | None:
    """预警入场：估值接近入场区 / 估值够低但曲线扁平或倒挂。"""
    pe = metrics["pe_ttm_pct_10y"]
    roll = metrics["roll_yield"]
    pe_in_zone = t.entry_pe_pct <= pe < t.warn_entry_pe_pct
    curve_flat = pe < t.entry_pe_pct and roll <= 0
    if pe_in_zone:
        roll_tag = (f"展期收益 {roll:+.1f}% > 0（曲线健康）"
                    if roll > 0
                    else f"展期收益 {roll:+.1f}% ≤ 0（曲线扁平/倒挂）")
        cond = (f"PE_TTM 分位 {pe:.1f}% 在 {t.entry_pe_pct}-{t.warn_entry_pe_pct}% 区间"
                f"（接近入场）；{roll_tag}")
    elif curve_flat:
        cond = (f"PE_TTM {pe:.1f}% < {t.entry_pe_pct}% 但 "
                f"展期收益 {roll:+.1f}% ≤ 0（曲线扁平/倒挂，展期吃不到价差）")
    else:
        return None
    return Signal(
        type="warn_entry", priority=4,
        condition=cond,
        current={"pe_ttm_pct_10y": pe, "roll_yield": roll},
        threshold={"warn_entry_pe_pct": t.warn_entry_pe_pct},
        suggestion="密切跟踪，等待曲线修复或估值进入入场区",
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
    roll_tag = (f"展期收益 {roll:+.1f}% > 0（曲线健康）"
                if roll > 0
                else f"展期收益 {roll:+.1f}% ≤ 0（曲线异常）")
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
    """退出条件 2：展期收益 ≤ 0（曲线扁平或倒挂）。

    roll_yield = 下月年化贴水 - 当月年化贴水。
    ≤ 0 表示远月不再比近月更深贴水（甚至升水），展期吃贴水策略前提失效。
    """
    roll = metrics["roll_yield"]
    if roll <= 0:
        return Signal(
            type="reduce", priority=1,
            condition=(f"展期收益 {roll:+.1f}% ≤ 0%（期限结构扁平或倒挂），"
                       f"展期吃贴水策略前提失效"),
            current={"roll_yield": roll},
            threshold={"exit_roll_yield": 0},
            suggestion="平仓——曲线倒挂状态下展期会反向亏钱",
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
