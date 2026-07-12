# valuation.py
from __future__ import annotations
from datetime import datetime, timedelta

DAYS_PER_YEAR = 365.25
WINDOW_DAYS = {"10y": 3652, "5y": 1826, "3y": 1096, "all": 99999}


def percentile(series: list[float], current: float) -> float:
    """计算 current 在 series 中的分位：<=current 的比例 x 100。
    空序列返回 0.0。
    """
    if not series:
        return 0.0
    count_le = sum(1 for v in series if v <= current)
    return count_le / len(series) * 100.0


def _filter_by_window(history: list[dict], field: str, days: int) -> list[float]:
    """按天数窗口过滤历史，提取数值字段。跳过 None/缺字段。"""
    cutoff = datetime.now() - timedelta(days=days)
    values = []
    for row in history:
        d = row.get("date")
        v = row.get(field)
        if d is None or v is None:
            continue
        try:
            row_date = datetime.strptime(str(d)[:10], "%Y-%m-%d")
        except (ValueError, TypeError):
            continue
        if row_date >= cutoff:
            values.append(float(v))
    return values


def compute_pct_for_windows(
    history: list[dict],
    current: dict,
    field: str,
    windows: list[str],
) -> dict[str, float]:
    """算多区间分位。返回 {window_name: pct}。
    history: 升序的估值行列表。
    current: 含 field 键的当前行。
    """
    current_val = current.get(field)
    if current_val is None:
        return {w: 0.0 for w in windows}
    current_val = float(current_val)

    result = {}
    for w in windows:
        days = WINDOW_DAYS.get(w, 99999)
        series = _filter_by_window(history, field, days)
        result[w] = percentile(series, current_val)
    return result


def pe_pb_divergence(pe_pct: float, pb_pct: float) -> float:
    """PE 分位 - PB 分位。正值=盈利低位，负值=净资产膨胀。"""
    return pe_pct - pb_pct
