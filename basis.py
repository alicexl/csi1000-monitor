# basis.py
from __future__ import annotations
from datetime import date
import calendar

QUARTER_MONTHS = [3, 6, 9, 12]


def compute_basis(futures_close: float, spot_close: float) -> float:
    """基差 = 期货收盘 - 现货收盘。负值=贴水，正值=升水。"""
    return futures_close - spot_close


def compute_annualized_discount(
    futures_close: float, spot_close: float, days_to_expire: int
) -> float:
    """年化贴水率 %。
    正值 = 买入期货到期收敛的年化收益（贴水）。
    负值 = 升水（买入期货亏损）。
    days_to_expire=0 返回 0 防除零。
    """
    if days_to_expire <= 0 or spot_close == 0:
        return 0.0
    discount_rate = (spot_close - futures_close) / spot_close * 100
    return discount_rate * 365 / days_to_expire


def third_friday(year: int, month: int) -> date:
    """计算某年月的第三个周五（中金所股指期货交割日）。"""
    cal = calendar.Calendar()
    fridays = [
        d for d in cal.itermonthdates(year, month)
        if d.month == month and d.weekday() == 4  # Friday=4
    ]
    return fridays[2]  # 第三个周五


def days_to_expire(today: date, expire_date: date) -> int:
    """剩余天数（今日到交割日的日历天数）。"""
    return (expire_date - today).days


def classify_contract(
    symbol: str, today: date
) -> tuple[str | None, date | None]:
    """识别 IM 合约类型（当月/下月/当季/下季）+ 交割日。
    非 IM 合约或格式错误返回 (None, None)。
    """
    if not symbol.startswith("IM") or len(symbol) != 6:
        return None, None
    try:
        yy = int(symbol[2:4])
        mm = int(symbol[4:6])
    except ValueError:
        return None, None

    if not (1 <= mm <= 12):
        return None, None

    year = 2000 + yy
    expire = third_friday(year, mm)

    today_yyyymm = today.year * 12 + today.month
    contract_yyyymm = year * 12 + mm

    # 当月
    if contract_yyyymm == today_yyyymm:
        return "当月", expire

    # 下月
    if contract_yyyymm == today_yyyymm + 1:
        return "下月", expire

    # 当季/下季：从"下个月"起找未来季月（确保不与当月/下月重复）
    future_quarters = []
    y, m = today.year, today.month
    m += 1  # 从下个月开始
    if m > 12:
        m = 1
        y += 1
    while len(future_quarters) < 4:
        if m in QUARTER_MONTHS:
            future_quarters.append((y, m))
        m += 1
        if m > 12:
            m = 1
            y += 1

    # future_quarters[0] = 当季, [1] = 下季
    for i, label in [(0, "当季"), (1, "下季")]:
        if i < len(future_quarters):
            qy, qm = future_quarters[i]
            if qy == year and qm == mm:
                return label, expire

    # 既不是当月/下月，也不是当季/下季 → 不在可交易合约范围
    return None, expire
