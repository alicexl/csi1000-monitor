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
def _entry_signal(metrics: dict, t: Thresholds) -> Signal | None:
    """入场：估值低 AND 有贴水（disc > 0）。"""
    pe = metrics["pe_ttm_pct_10y"]
    disc = metrics["current_month_discount"]
    if pe < t.entry_pe_pct and disc > 0:
        return Signal(
            type="entry", priority=2,
            condition=(f"PE_TTM 10y分位 {pe:.1f}% < {t.entry_pe_pct}% "
                       f"且 当月年化贴水 {disc:.1f}% > 0（有贴水可吃）"),
            current={"pe_ttm_pct_10y": pe, "discount": disc},
            threshold={"entry_pe_pct": t.entry_pe_pct},
            suggestion="买入当月 IM 合约入场",
        )
    return None


def _warn_entry_signal(metrics: dict, t: Thresholds) -> Signal | None:
    """预警入场：估值接近入场区 OR 估值够低但期货升水。"""
    pe = metrics["pe_ttm_pct_10y"]
    disc = metrics["current_month_discount"]
    pe_in_zone = t.entry_pe_pct <= pe < t.warn_entry_pe_pct
    premium_state = pe < t.entry_pe_pct and disc <= 0
    if pe_in_zone:
        disc_tag = (f"贴水 {disc:.1f}% > 0（已达标）"
                    if disc > 0
                    else f"贴水 {disc:.1f}% ≤ 0（升水，不宜入场）")
        cond = (f"PE_TTM 分位 {pe:.1f}% 在 {t.entry_pe_pct}-{t.warn_entry_pe_pct}% 区间"
                f"（接近入场）；{disc_tag}")
    elif premium_state:
        cond = (f"PE_TTM {pe:.1f}% < {t.entry_pe_pct}% 但 "
                f"贴水 {disc:.1f}% ≤ 0（期货升水，吃贴水策略失效）")
    else:
        return None
    return Signal(
        type="warn_entry", priority=4,
        condition=cond,
        current={"pe_ttm_pct_10y": pe, "discount": disc},
        threshold={"warn_entry_pe_pct": t.warn_entry_pe_pct},
        suggestion="密切跟踪，等待贴水修复或估值进入入场区",
    )


def _wait_signal(metrics: dict, t: Thresholds) -> Signal:
    pe = metrics["pe_ttm_pct_10y"]
    disc = metrics["current_month_discount"]
    if pe >= t.reduce_pe_pct:
        zone = f"过高（≥{t.reduce_pe_pct}%），等待估值回落"
    elif pe >= t.warn_reduce_pe_pct:
        zone = f"偏高（{t.warn_reduce_pe_pct}-{t.reduce_pe_pct}%），不宜入场"
    else:
        zone = f"观望区（{t.warn_entry_pe_pct}-{t.warn_reduce_pe_pct}%）"
    disc_tag = (f"贴水 {disc:.1f}% > 0（可吃）"
                if disc > 0
                else f"贴水 {disc:.1f}% ≤ 0（升水）")
    return Signal(
        type="wait", priority=5,
        condition=f"PE_TTM 分位 {pe:.1f}% {zone}；{disc_tag}",
        current={"pe_ttm_pct_10y": pe, "discount": disc},
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
    """退出条件 2：贴水变升水（disc ≤ 0）。策略前提失效。"""
    disc = metrics["current_month_discount"]
    if disc <= 0:
        return Signal(
            type="reduce", priority=1,
            condition=(f"当月年化贴水 {disc:.1f}% ≤ 0%（期货转升水），"
                       f"吃贴水策略前提失效"),
            current={"discount": disc},
            threshold={"exit_discount": 0},
            suggestion="平仓——升水状态下持有 IM 多头会反向亏钱",
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
    disc = metrics["current_month_discount"]
    if pe <= t.warn_reduce_pe_pct and days >= t.switch_days and disc > 0:
        return Signal(
            type="hold", priority=5,
            condition=(f"PE_TTM {pe:.1f}% ≤ {t.warn_reduce_pe_pct}% 且 "
                       f"剩余 {days} 天 ≥ {t.switch_days} 且 "
                       f"贴水 {disc:.1f}% > 0"),
            current={"pe_ttm_pct_10y": pe, "days_to_expire": days, "discount": disc},
            threshold={"warn_reduce_pe_pct": t.warn_reduce_pe_pct, "switch_days": t.switch_days},
            suggestion="继续持有吃贴水",
        )
    return None


def evaluate(
    state: str, metrics: dict[str, Any], thresholds: Thresholds
) -> list[Signal]:
    """根据持仓状态 + 指标 + 阈值 → 返回信号列表（已按 priority 排序）。"""
    sigs: list[Signal] = []

    if state == "empty":
        for fn in (_entry_signal, _warn_entry_signal, _wait_signal):
            s = fn(metrics, thresholds)
            if s is not None:
                sigs.append(s)
    elif state == "holding":
        for fn in (_reduce_pe_signal, _reduce_basis_signal, _warn_reduce_signal,
                   _switch_signal, _hold_signal):
            s = fn(metrics, thresholds)
            if s is not None:
                sigs.append(s)
    else:
        sigs.append(Signal(
            type="wait", priority=5,
            condition=f"未知持仓状态: {state}",
            current={}, threshold={},
            suggestion="检查 monitor.py 的 POSITION.status",
        ))

    sigs.sort(key=lambda s: s.priority)
    return sigs
