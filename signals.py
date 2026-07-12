# signals.py
from __future__ import annotations
from dataclasses import dataclass
from typing import Any

from config import Thresholds


@dataclass
class Signal:
    type: str           # entry/warn_entry/reduce/warn_reduce/switch/wait/hold
    priority: int       # 1(最高) ~ 5
    condition: str
    current: dict
    threshold: dict
    suggestion: str


def _entry_signal(metrics: dict, t: Thresholds) -> Signal | None:
    pe = metrics["pe_ttm_pct_10y"]
    disc = metrics["current_month_discount"]
    if pe < t.entry_pe_pct and disc > t.entry_discount:
        return Signal(
            type="entry", priority=2,
            condition=f"PE_TTM 10y分位 {pe:.1f}% < {t.entry_pe_pct}% 且 当月年化贴水 {disc:.1f}% > {t.entry_discount}%",
            current={"pe_ttm_pct_10y": pe, "discount": disc},
            threshold={"entry_pe_pct": t.entry_pe_pct, "entry_discount": t.entry_discount},
            suggestion="买入当月 IM 合约入场",
        )
    return None


def _warn_entry_signal(metrics: dict, t: Thresholds) -> Signal | None:
    pe = metrics["pe_ttm_pct_10y"]
    disc = metrics["current_month_discount"]
    pe_in_zone = t.entry_pe_pct <= pe < t.warn_entry_pe_pct
    discount_low = pe < t.entry_pe_pct and disc <= t.entry_discount
    if pe_in_zone or discount_low:
        if pe_in_zone:
            cond = f"PE_TTM 分位 {pe:.1f}% 在 {t.entry_pe_pct}-{t.warn_entry_pe_pct}% 区间（接近入场）"
        else:
            cond = f"PE_TTM {pe:.1f}% < {t.entry_pe_pct}% 但贴水 {disc:.1f}% ≤ {t.entry_discount}%（贴水不够）"
        return Signal(
            type="warn_entry", priority=4,
            condition=cond,
            current={"pe_ttm_pct_10y": pe, "discount": disc},
            threshold={"warn_entry_pe_pct": t.warn_entry_pe_pct, "entry_discount": t.entry_discount},
            suggestion="密切跟踪，准备入场",
        )
    return None


def _wait_signal(metrics: dict, t: Thresholds) -> Signal:
    pe = metrics["pe_ttm_pct_10y"]
    return Signal(
        type="wait", priority=5,
        condition=f"PE_TTM 分位 {pe:.1f}% ≥ {t.warn_entry_pe_pct}%，未达入场区",
        current={"pe_ttm_pct_10y": pe},
        threshold={"warn_entry_pe_pct": t.warn_entry_pe_pct},
        suggestion="继续等待，不需要操作",
    )


def _reduce_signal(metrics: dict, t: Thresholds) -> Signal | None:
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
    if pe <= t.warn_reduce_pe_pct and days >= t.switch_days:
        return Signal(
            type="hold", priority=5,
            condition=f"PE_TTM {pe:.1f}% ≤ {t.warn_reduce_pe_pct}% 且 剩余 {days} 天 ≥ {t.switch_days}",
            current={"pe_ttm_pct_10y": pe, "days_to_expire": days},
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
        for fn in (_reduce_signal, _warn_reduce_signal, _switch_signal, _hold_signal):
            s = fn(metrics, thresholds)
            if s is not None:
                sigs.append(s)
    else:
        sigs.append(Signal(
            type="wait", priority=5,
            condition=f"未知持仓状态: {state}",
            current={}, threshold={},
            suggestion="检查 config.yaml 的 position.status",
        ))

    sigs.sort(key=lambda s: s.priority)
    return sigs
