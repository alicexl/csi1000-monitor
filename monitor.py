# monitor.py
from __future__ import annotations
import argparse
import math
import sys
from pathlib import Path
from datetime import date, datetime, timedelta

from db import (
    init_db, upsert_valuation, upsert_contract, insert_signal,
    query_latest_valuation, query_valuation_history,
    query_contracts_by_date, query_main_continuous_history,
    load_position, save_position,
)
from data_fetcher import fetch_valuation, fetch_main_continuous, fetch_daily_contracts, fetch_otm_call
from signals import evaluate, score_carry, Thresholds, Position
from reporter import generate_report, render_status_line


# ─── 多区间分位算法（原 valuation.py）─────────────────────────
WINDOW_DAYS = {"10y": 3652, "5y": 1826, "all": 99999}
MIN_SAMPLES = 100  # 绝对阈值（约 5 个月交易日），低于此分位直接 N/A


def percentile(series: list[float], current: float) -> float:
    """current 在 series 中的分位：<=current 的比例 × 100。空序列返回 0.0。"""
    if not series:
        return 0.0
    count_le = sum(1 for v in series if v <= current)
    return count_le / len(series) * 100.0


def _filter_by_window(history: list[dict], field: str, days: int) -> list[float]:
    """按天数窗口过滤历史，提取数值字段。cutoff 用今日午夜，避免 now 的时刻比较 bug。"""
    today_midnight = datetime.now().replace(
        hour=0, minute=0, second=0, microsecond=0)
    cutoff = today_midnight - timedelta(days=days)
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
    history: list[dict], current: dict, field: str, windows: list[str],
) -> dict[str, dict]:
    """算多区间分位。返回 {window_name: {pct, n}}。

    - pct: 分位值；样本不足（n < MIN_SAMPLES）时为 None
    - n: 实际样本数
    """
    current_val = current.get(field)
    if current_val is None:
        return {w: {"pct": None, "n": 0} for w in windows}
    current_val = float(current_val)
    result = {}
    for w in windows:
        days = WINDOW_DAYS.get(w, 99999)
        series = _filter_by_window(history, field, days)
        n = len(series)
        pct = percentile(series, current_val) if n >= MIN_SAMPLES else None
        result[w] = {"pct": pct, "n": n}
    return result


def pe_pb_divergence(pe_pct: float, pb_pct: float) -> float:
    """PE 分位 - PB 分位。

    正值 = PE 相对 PB 更贵（E 弱 + B 强）→ 盈利阶段性低位；
    负值 = PB 相对 PE 更贵（E 强 + B 弱）→ 盈利强劲或净资产收缩。
    PB 高 ≠ 净资产高；PB 高 = 单位净资产卖得贵 = B 相对 P 偏低。
    """
    return pe_pct - pb_pct


# ─── PBS 底部回归（原 bottom_trend.py）─────────────────────────
# PBS = close / pb ≈ 指数隐含净资产 B。对 PBS 的历史局部低点做对数回归
# ln(PBS) = a + b*t，拟合"净资产底部抬升趋势线"。当前 PBS 距趋势线的 %
# 反映长期估值底视角：低于趋势线 = 长期便宜，高于 = 长期偏贵。
#
# 用 PBS 而非纯点位：PBS 剥离了 PE/PB 周期波动，只保留净资产复利增长，
# 对数回归 R²≈0.90（vs 纯点位 0.46），底部抬升规律性远优于点位。
BOTTOM_WINDOW_DAYS = 20  # 局部低点检测窗口（±交易日）；±20 → 31 个低点，R²≈0.91
BOTTOM_MIN_POINTS = 3   # 拟合最少低点数，不足返回 None
BOTTOM_START_DATE = "2014-10-17"  # PB 数据起点（指数上市首日）；此前无官方 PB

# PB 压缩空间情景：固定资产 B（=当前 PBS），看不同 PB 分位下的点位与跌幅。
# 用历史 PB 分位（而非固定值）定义情景：当前 → 50%中位 → 25% → 10%低估，
# 跨周期可比，避免固定值（如 1.5）随时间失效。
PB_COMPRESSION_PERCENTILES = [50, 25, 10]


def compute_pbs(close: float, pb: float) -> float:
    """PBS = 指数点位 / 市净率 ≈ 隐含净资产 B。pb<=0 返回 0（无意义）。"""
    if pb <= 0:
        return 0.0
    return close / pb


def percentile_value(series: list[float], pct: float) -> float | None:
    """反查分位对应的值：pct% 分位 → series 中第 pct/100*n 大的值。

    与 percentile()（当前值在序列中的分位）互为反函数。series 升序。
    样本不足返回 None。
    """
    if not series:
        return None
    s = sorted(series)
    idx = min(int(pct / 100 * len(s)), len(s) - 1)
    return s[idx]


def pb_compression_scenarios(
    current_close: float, current_pb: float,
    pb_history: list[float] | None = None,
    pcts: list[int] = PB_COMPRESSION_PERCENTILES,
) -> list[dict] | None:
    """PB 压缩空间：固定资产 B=close/pb，算各 PB 分位情景对应点位与跌幅。

    逻辑：P = B × PB，B 固定（当前净资产），PB 越低 → P 越低。
    情景用历史 PB 分位定义（50%中位/25%/10%低估），跨周期可比。
    返回 [{pb, price, drop_pct, tag}, ...] 含当前行 + 各分位情景，或 None。
    drop_pct = (price - current) / current * 100（负值=跌幅）。
    无历史 PB 时退化为只用当前行。
    """
    if current_pb <= 0 or current_close <= 0:
        return None
    book = current_close / current_pb  # 资产 B 固定
    rows = [{"pb": current_pb, "price": current_close, "drop_pct": 0.0,
             "tag": "当前"}]
    if pb_history:
        for pct in pcts:
            pb = percentile_value(pb_history, pct)
            if pb is None:
                continue
            price = book * pb
            drop = (price - current_close) / current_close * 100
            rows.append({"pb": pb, "price": price, "drop_pct": drop,
                         "tag": f"PB {pct}%分位"})
    return rows


# 底部回归理论点位：PBS 偏离比分位（历史 PBS 相对当日趋势线的偏离 = PBS_now/PBS_fair）
THEORY_DEV_PERCENTILES = [50, 25, 10]


def _compute_theory_points(
    dev_ratios: list[float], trend_now: float, current_pb: float,
    current_pbs: float,
) -> list[dict] | None:
    """回归框架下理论点位：偏离比分位 × 当前趋势线 × 当前 PB。

    dev_ratios: 历史 PBS_now/PBS_fair 偏离比序列（>1 高于趋势，<1 低于）。
    理论点位 = 趋势线值(trend_now) × 偏离比分位 × 当前 PB。
    返回 [{ratio, price, drop_pct, tag}, ...] 含纯趋势线 + 各分位 + 当前偏离行。
    与 PB 分位情景点位对称：那块固定 B 看 PB 分位，这块固定 PB 看 PBS 偏离分位。
    """
    if not dev_ratios or trend_now <= 0 or current_pb <= 0:
        return None
    rows = []
    # 纯趋势线（偏离=1.0）= 资产沿长期增长趋势的公允点位
    rows.append({
        "ratio": 1.0, "price": trend_now * current_pb,
        "drop_pct": 0.0, "tag": "回归趋势线",
    })
    for pct in THEORY_DEV_PERCENTILES:
        ratio = percentile_value(dev_ratios, pct)
        if ratio is None:
            continue
        price = trend_now * ratio * current_pb
        rows.append({
            "ratio": ratio, "price": price,
            "drop_pct": 0.0, "tag": f"PBS偏离{pct}%分位",
        })
    # 当前偏离行（drop_pct 相对纯趋势线点位）
    base_price = trend_now * current_pb
    for r in rows:
        r["drop_pct"] = (r["price"] - base_price) / base_price * 100 if base_price > 0 else 0.0
    # 当前 PBS 对应点位（用真实 close 反推，便于对照）
    rows.append({
        "ratio": current_pbs / trend_now if trend_now > 0 else 0.0,
        "price": current_pbs * current_pb,
        "drop_pct": (current_pbs * current_pb - base_price) / base_price * 100 if base_price > 0 else 0.0,
        "tag": "当前",
    })
    return rows


def detect_local_lows(
    points: list[tuple[str, float]], window: int = BOTTOM_WINDOW_DAYS,
) -> list[tuple[str, float]]:
    """检测局部低点：每个点在 ±window 窗口内是最低值。

    points: [(date_str, value), ...] 按日期升序。窗口两端不足的点跳过。
    牛市回调的"伪低点"会被宽窗口过滤（±60 日 ≈ 季度级，只留中级以上底）。
    """
    n = len(points)
    if n < 2 * window + 1:
        return []
    lows = []
    for i in range(window, n - window):
        win_min = min(points[j][1] for j in range(i - window, i + window + 1))
        if points[i][1] == win_min:
            lows.append(points[i])
    return lows


def fit_bottom_trend(
    lows: list[tuple[str, float]], window: int = BOTTOM_WINDOW_DAYS,
) -> dict | None:
    """对 PBS 低点做对数回归 ln(value) = a + b*date_ordinal。

    返回 {a, b, r2, annual_pct, trend_now, n, window} 或 None（低点不足）。
    - annual_pct: 趋势线年化抬升率 = b*365*100（对数回归下即净资产年化增速）
    - trend_now: 当前日期的趋势线值 = exp(a + b*t_today)
    - window: 低点检测窗口（供 reporter 展示，避免跨模块依赖常量）
    """
    if len(lows) < BOTTOM_MIN_POINTS:
        return None
    xs, ys = [], []
    for d, v in lows:
        if v <= 0:
            continue
        xs.append(datetime.strptime(d[:10], "%Y-%m-%d").toordinal())
        ys.append(math.log(v))
    if len(xs) < BOTTOM_MIN_POINTS:
        return None

    n = len(xs)
    mx = sum(xs) / n
    my = sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    b = sxy / sxx
    a = my - b * mx
    ss_tot = sum((y - my) ** 2 for y in ys)
    ss_res = sum((y - (a + b * x)) ** 2 for x, y in zip(xs, ys))
    r2 = 1 - ss_res / ss_tot if ss_tot > 0 else 0.0

    today_ord = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).toordinal()
    trend_now = math.exp(a + b * today_ord)
    return {
        "a": a, "b": b, "r2": r2,
        "annual_pct": b * 365 * 100,
        "trend_now": trend_now,
        "n": n,
        "window": window,
    }


# 预期收益计算的默认假设（来自杨康平《股指期货吃贴水策略》PDF 经验值）
DEFAULT_DIVIDEND_YIELD = 1.0  # 中证1000 近年股息率约 1-2%，取保守下限


def _window_median(history: list[dict], field: str, days: int) -> float | None:
    """指定天数窗口内某字段的中位数（cutoff 用今日午夜，与 _filter_by_window 一致）。"""
    today_midnight = datetime.now().replace(
        hour=0, minute=0, second=0, microsecond=0)
    cutoff = today_midnight - timedelta(days=days)
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
    if not values:
        return None
    values.sort()
    n = len(values)
    mid = n // 2
    if n % 2 == 1:
        return values[mid]
    return (values[mid - 1] + values[mid]) / 2


def _compute_expected_return(
    close: float, pe_ttm: float, pb: float,
    pe_median_10y: float | None,
    dividend_yield: float = DEFAULT_DIVIDEND_YIELD,
) -> dict:
    """三因子预期收益模型（杨康平 PDF 框架）：

        预期年化 = ROE + 分红 + 估值变动

    - ROE = PB / PE = (P/B) / (P/E) = E/B（不需要额外数据源，用现有 PE/PB 反推）
    - 分红率：默认 1.0%（PDF 经验值，中证1000 历史约 1-2%）
    - 估值回归：如果 PE 回到 10 年中位数，估值变动 = (pe_median - pe_now) / pe_now
      （正值 = 低估有修复空间；负值 = 高估有回落风险）

    **展期收益不计入此 panel**：吃贴水策略的核心收益来自曲线斜率（roll_yield =
    下月年化贴水 - 当月年化贴水），但该值随期限结构变化、难以多年预测。用户可参考
    期货合约表的基差/年化贴水直观判断当前曲线健康度。status_line 一行单独展示 roll_yield。

    返回 dict：各分量 + 估值不变年化 + 3年/5年复利 + 估值回归 1 年预期。
    """
    if pe_ttm <= 0 or pb <= 0:
        return {
            "roe_pct": 0.0, "dividend_yield_pct": dividend_yield,
            "pe_median_10y": pe_median_10y,
            "valuation_change_pct": 0.0,
            "annual_no_valuation_pct": dividend_yield,
            "c3y_no_valuation_pct": 0.0, "c5y_no_valuation_pct": 0.0,
            "annual_with_mean_reversion_pct": dividend_yield,
        }

    roe = pb / pe_ttm * 100  # E/B = (P/B)/(P/E) 百分比形式
    div = dividend_yield
    base = roe + div  # 估值不变时的年化（%）

    if pe_median_10y and pe_median_10y > 0:
        val_change = (pe_median_10y - pe_ttm) / pe_ttm * 100
    else:
        val_change = 0.0

    c3y = ((1 + base / 100) ** 3 - 1) * 100
    c5y = ((1 + base / 100) ** 5 - 1) * 100

    return {
        "roe_pct": roe,
        "dividend_yield_pct": div,
        "pe_median_10y": pe_median_10y,
        "valuation_change_pct": val_change,
        "annual_no_valuation_pct": base,
        "c3y_no_valuation_pct": c3y,
        "c5y_no_valuation_pct": c5y,
        "annual_with_mean_reversion_pct": base + val_change,
    }


ROOT = Path(__file__).parent
DB_PATH = ROOT / "csi1000_monitor.db"
REPORTS_DIR = ROOT / "reports"

# ─── 策略阈值（需要调参时改这里）─────────────────────────────
THRESHOLDS = Thresholds()

# 估值分位计算窗口
PCT_WINDOWS = ["10y", "5y", "all"]


def _load_position(conn) -> Position:
    """从 DB 加载持仓状态；空表返回默认 Position()。"""
    row = load_position(conn)
    if row is None:
        return Position()
    return Position(
        status=row["status"],
        contract=row["contract"],
        entry_date=row["entry_date"],
        entry_price=row["entry_price"],
    )


def _resolve_trade_date(spot_close: float,
                        max_lookback: int = 7) -> tuple[date, list[dict]]:
    """返回 (交易日, 当日合约列表)。从今天起向前回退最多 max_lookback 天。

    CFFEX 当日数据通常要下午 3 点后才发布；凌晨/早盘跑 scan 时自动用上一交易日。
    顺带把合约数据带回，避免 _scan 二次拉取。
    """
    for i in range(max_lookback + 1):
        d = date.today() - timedelta(days=i)
        contracts = fetch_daily_contracts(d, spot_close)
        if contracts:
            return d, contracts
    return (date.today(), [])


def _extract_signal_metrics(metrics: dict) -> dict:
    """从 metrics 抽出 signals.evaluate 需要的指标 dict。

    策略判断基于**展期收益 roll_yield = 下月年化贴水 - 当月年化贴水**（期限结构斜率）：
    roll_yield > 0 表示远月比近月更深贴水（曲线向下倾斜），展期（卖近月买远月）能吃到价差。
    当月/下月绝对贴水保留作展示参考（status_line/报告）。
    days 用于 switch 信号（当月临交割时切月）。
    """
    contracts = metrics.get("contracts", [])
    cur_month = next(
        (c for c in contracts if c["contract_type"] == "当月"), None)
    next_month = next(
        (c for c in contracts if c["contract_type"] == "下月"), None)
    d_near = cur_month["annualized_discount"] if cur_month else 0
    d_far = next_month["annualized_discount"] if next_month else 0
    return {
        "pe_ttm_pct_10y": metrics["pe_ttm_pct"].get("10y", {}).get("pct")
                          or 100,  # 样本不足（None）→ 100，保守 wait 不入场
        "pb_pct_10y": metrics.get("pb_pct", {}).get("10y", {}).get("pct"),  # None → 不入场
        "pbs_score": (metrics.get("bottom_trend") or {}).get("pbs_score"),  # None → 不入场
        "current_month_discount": d_near,
        "current_month_days": cur_month["days_to_expire"] if cur_month else 999,
        "next_month_discount": d_far,
        "roll_yield": d_far - d_near,
    }


def _target_trade_date() -> date:
    """目标交易日：周末回退到周五（节假日由 akshare 自然返回空数据）。"""
    today = date.today()
    if today.weekday() == 5:  # Sat
        return today - timedelta(days=1)
    if today.weekday() == 6:  # Sun
        return today - timedelta(days=2)
    return today


def _scan() -> int:
    """拉数据入库。返回 0 成功 / 非 0 失败。"""
    conn = init_db(DB_PATH)

    # 1. 拉估值
    print("[1/3] 拉取 PE/PB 历史...", flush=True)
    val_rows = fetch_valuation()
    val_ins = val_upd = 0
    for r in val_rows:
        if upsert_valuation(conn, r) == "inserted":
            val_ins += 1
        else:
            val_upd += 1
    print(f"      OK {len(val_rows)} 行（新增 {val_ins}，更新 {val_upd}）", flush=True)

    # 2. 拉主力连续（需要最新现货收盘算基差）
    latest = query_latest_valuation(conn)
    if latest is None:
        print("[ERR] 无估值数据，无法拉期货", file=sys.stderr)
        return 1
    spot_close = latest["close"]

    today, cached_contracts = _resolve_trade_date(spot_close)
    if today != date.today():
        print(f"[INFO] 今日 CFFEX 数据未发布，使用 {today}", file=sys.stderr)

    print("[2/3] 拉取主力连续 IM0...", flush=True)
    # 用各历史日期对应的现货收盘算基差（不能用今天的现货，否则历史 basis 全部偏大）
    val_hist = query_valuation_history(conn, days=99999)
    spot_by_date = {r["date"]: r["close"] for r in val_hist if r.get("close")}
    main_rows = fetch_main_continuous(spot_by_date, today)
    main_ins = main_upd = 0
    for r in main_rows:
        if upsert_contract(conn, r) == "inserted":
            main_ins += 1
        else:
            main_upd += 1
    print(f"      OK {len(main_rows)} 行（新增 {main_ins}，更新 {main_upd}）", flush=True)

    # 3. 入库当日 IM 合约（复用 _resolve_trade_date 的结果）
    print(f"[3/3] 入库 IM 合约（{today}）...", flush=True)
    ct_ins = ct_upd = 0
    for r in cached_contracts:
        if upsert_contract(conn, r) == "inserted":
            ct_ins += 1
        else:
            ct_upd += 1
    print(f"      OK {len(cached_contracts)} 合约（新增 {ct_ins}，更新 {ct_upd}）", flush=True)

    conn.close()
    return 0


def _compute_bottom_trend(
    history: list[dict], current_close: float, current_pb: float,
) -> dict | None:
    """从估值历史构造 PBS 序列 → 检测低点 → 对数回归 → 补当前 PBS 指标。

    只用 BOTTOM_START_DATE（2014-10-17）之后的数据——此前无官方 PB。
    返回 fit_bottom_trend 结果 + 当前 PBS + pbs_score + 最近低点 + 理论点位 +
    PB 分位情景点位，或 None。

    pbs_score = PBS_fair / PBS_now：>1 资产折价(便宜)，=1 正常，<1 资产溢价(贵)。
    """
    points = []
    pb_history = []
    for r in history:
        d = r.get("date")
        c = r.get("close")
        pb = r.get("pb")
        if d is None or c is None or pb is None or pb <= 0:
            continue
        if str(d)[:10] < BOTTOM_START_DATE:
            continue
        points.append((str(d)[:10], c / pb))
        pb_history.append(pb)
    if len(points) < 2 * BOTTOM_WINDOW_DAYS + 1:
        return None

    lows = detect_local_lows(points, BOTTOM_WINDOW_DAYS)
    fit = fit_bottom_trend(lows)
    if fit is None:
        return None

    cur_pbs = compute_pbs(current_close, current_pb)
    fit["current_pbs"] = cur_pbs
    # PBS_score = 趋势线 / 当前，>1 便宜 <1 贵
    fit["pbs_score"] = fit["trend_now"] / cur_pbs if cur_pbs > 0 else 0.0
    fit["recent_low"] = lows[-1] if lows else None

    # 回归框架下理论点位：PBS 相对当日趋势线的偏离比 (=PBS_now/PBS_fair) 的
    # 历史分位 × 当前趋势线值 × 当前 PB。偏离比 >1 高于趋势(贵)，<1 低于(便宜)。
    # 与 PB 分位情景点位对称：那块固定 B 看 PB 分位，这块固定 PB 看 PBS 偏离分位。
    a, b = fit["a"], fit["b"]
    ratios = []
    for d, v in points:
        try:
            t_ord = datetime.strptime(d, "%Y-%m-%d").toordinal()
            fair = math.exp(a + b * t_ord)
            if fair > 0:
                ratios.append(v / fair)
        except (ValueError, OverflowError):
            continue
    fit["theory_points"] = _compute_theory_points(
        ratios, fit["trend_now"], current_pb, cur_pbs)

    # PB 压缩空间：固定资产 B，各 PB 情景对应点位与跌幅
    fit["pb_compression"] = pb_compression_scenarios(
        current_close, current_pb, pb_history)
    return fit


def _next_quarter_discount(contracts: list[dict]) -> float | None:
    """取"下季"合约的年化贴水（如 IM2612）。

    下季合约代表较长期限的贴水水平，比当月/下月更稳定，适合做 Carry Score 的
    收益锚点。无下季合约时回退下月，再回退当月；都没有返回 None。
    """
    for ctype in ("下季", "下月", "当月"):
        c = next((x for x in contracts if x.get("contract_type") == ctype), None)
        if c and c.get("annualized_discount") is not None:
            return c["annualized_discount"]
    return None


def _compute_carry_score(
    contracts: list[dict],
    pb_pct_10y: float | None,
    pbs_score: float | None,
    t: Thresholds = THRESHOLDS,
) -> dict | None:
    """组装 IM Carry Score。三因子缺任一（如下季贴水或 PBS 未算出）返回 None。"""
    discount = _next_quarter_discount(contracts)
    # 三因子任一缺失则不评分（避免用默认值误导）
    if discount is None or pb_pct_10y is None or pbs_score is None:
        return None
    cs = score_carry(discount, pb_pct_10y, pbs_score, t)
    return {
        "total": cs.total,
        "discount_pts": cs.discount_pts,
        "pb_pts": cs.pb_pts,
        "pbs_pts": cs.pbs_pts,
        "discount_value": cs.discount_value,
        "pb_pct": cs.pb_pct,
        "pbs_score": cs.pbs_score,
        "band": cs.band,
    }


def _build_metrics(conn) -> dict:
    """从 DB 拉数据，组装 metrics dict 供 signals/reporter 使用。"""
    latest = query_latest_valuation(conn)
    if latest is None:
        return {}
    history = query_valuation_history(conn, days=3650)
    contracts = query_contracts_by_date(conn, latest["date"])

    # 多区间分位
    pe_ttm_pct = compute_pct_for_windows(history, latest, "pe_ttm", PCT_WINDOWS)
    pb_pct = compute_pct_for_windows(history, latest, "pb", PCT_WINDOWS)

    pe_ttm = latest.get("pe_ttm") or 0
    pb = latest.get("pb") or 0
    close = latest.get("close") or 0

    # 10 年窗口 PE_TTM 中位数（用于估值回归预期）
    pe_median_10y = _window_median(history, "pe_ttm", WINDOW_DAYS["10y"])

    # PBS 底部回归：close/pb ≈ 隐含净资产，对历史局部低点对数回归拟合底部抬升趋势线
    bottom_trend = _compute_bottom_trend(history, close, pb)

    # 主力连续贴水分位（近2年）
    main_hist = query_main_continuous_history(conn, days=730)
    main_bases = [abs(r["basis"]) for r in main_hist if r.get("basis") is not None]
    cur_main = main_hist[-1] if main_hist else None
    if cur_main and main_bases:
        cur_main_abs = abs(cur_main.get("basis", 0))
        main_pct = sum(1 for b in main_bases if b <= cur_main_abs) / len(main_bases) * 100
    else:
        main_pct = None

    metrics = {
        "date": latest["date"],
        "close": close,
        "pe_ttm": pe_ttm,
        "pe_static": latest.get("pe_static") or 0,
        "pb": pb,
        "pe_ttm_pct": pe_ttm_pct,
        "pb_pct": pb_pct,
        "eps_ttm": close / pe_ttm if pe_ttm else 0,
        "bps": close / pb if pb else 0,
        "pe_pb_divergence": pe_pb_divergence(
            pe_ttm_pct.get("10y", {}).get("pct") or 0,
            pb_pct.get("10y", {}).get("pct") or 0),
        "contracts": contracts,
        "main_continuous_discount_pct": main_pct,
        "bottom_trend": bottom_trend,
    }

    # IM Carry Score（下季贴水 + PB 分位 + PBS_score，满分 100）
    metrics["carry_score"] = _compute_carry_score(
        contracts,
        pb_pct.get("10y", {}).get("pct"),
        bottom_trend.get("pbs_score") if bottom_trend else None,
    )

    # 预期收益三因子（PDF 框架：ROE + 分红 + 估值变动；展期收益单独看 roll_yield）
    metrics["expected_return"] = _compute_expected_return(
        close, pe_ttm, pb, pe_median_10y)

    # 期权增厚分析（实时拉取，失败不阻断）
    try:
        metrics["otm_call"] = fetch_otm_call(
            close, date.today(), otm_pct=10.0,
            switch_days=THRESHOLDS.switch_days)
    except Exception:
        metrics["otm_call"] = None
    return metrics


def _generate_report() -> int:
    """读 DB → 评估信号 → 写 signals 表 + 生成 Markdown 报告。"""
    conn = init_db(DB_PATH)
    position = _load_position(conn)
    metrics = _build_metrics(conn)
    if not metrics:
        print("[ERR] DB 无数据，请先运行 run", file=sys.stderr)
        return 1

    sigs = evaluate(position.status, _extract_signal_metrics(metrics), THRESHOLDS)

    # 写入 signals 表
    for s in sigs:
        insert_signal(conn, {
            "date": metrics["date"],
            "signal_type": s.type,
            "condition": s.condition,
            "current_value": str(s.current),
            "threshold": str(s.threshold),
            "suggestion": s.suggestion,
            "created_at": datetime.now().isoformat(timespec="seconds"),
        })

    # 生成报告
    report = generate_report(metrics["date"], position, metrics, sigs)
    REPORTS_DIR.mkdir(exist_ok=True)
    out_path = REPORTS_DIR / f"csi1000_{metrics['date']}.md"
    out_path.write_text(report, encoding="utf-8")
    print(f"[OK] 报告已生成: {out_path}")

    conn.close()
    return 0


def cmd_status() -> int:
    conn = init_db(DB_PATH)
    position = _load_position(conn)
    metrics = _build_metrics(conn)
    if not metrics:
        print("[ERR] DB 无数据，请先运行 run", file=sys.stderr)
        return 1

    sig_metrics = _extract_signal_metrics(metrics)
    sigs = evaluate(position.status, sig_metrics, THRESHOLDS)
    top = min(sigs, key=lambda s: s.priority) if sigs else None
    sig_type = top.type if top else "none"

    print(render_status_line(metrics["date"], position, metrics, sig_type,
                             sig_metrics["roll_yield"]))
    conn.close()
    return 0


def cmd_run() -> int:
    """DB 数据不是最新（目标交易日）则先拉数据，再生成报告。"""
    conn = init_db(DB_PATH)
    latest = query_latest_valuation(conn)
    target = _target_trade_date()
    db_date = None
    if latest:
        try:
            db_date = datetime.strptime(str(latest["date"]), "%Y-%m-%d").date()
        except ValueError:
            db_date = None
    if db_date and db_date >= target:
        print(f"[INFO] DB 已是最新（{db_date}），跳过数据拉取",
              flush=True)
        conn.close()
    else:
        conn.close()
        rc = _scan()
        if rc:
            return rc
    return _generate_report()


def cmd_open(args) -> int:
    """开仓：写入 status=holding + contract/entry_price/entry_date。"""
    conn = init_db(DB_PATH)
    entry_date = args.entry_date or date.today().isoformat()
    save_position(conn, {
        "status": "holding",
        "contract": args.contract,
        "entry_date": entry_date,
        "entry_price": args.entry_price,
        "updated_at": datetime.now().isoformat(timespec="seconds"),
    })
    print(f"[OK] 已开仓: {args.contract} @ {args.entry_price} ({entry_date})")
    conn.close()
    return 0


def cmd_close(args) -> int:
    """平仓：写入 status=empty，清空合约信息。"""
    conn = init_db(DB_PATH)
    save_position(conn, {
        "status": "empty",
        "contract": None,
        "entry_date": None,
        "entry_price": None,
        "updated_at": datetime.now().isoformat(timespec="seconds"),
    })
    print("[OK] 已平仓")
    conn.close()
    return 0


def main() -> int:
    # Windows 控制台默认 GBK 编码，无法输出 emoji；status_line 和报告里的 emoji 需要 UTF-8
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(
        description="中证1000 贴水策略监控")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("run", help="自动拉数据 + 生成报告（DB 是最新则跳过拉取）")
    sub.add_parser("status", help="一行快速查状态（离线）")

    p_open = sub.add_parser("open", help="开仓：记录合约/入场价/日期")
    p_open.add_argument("contract", help="合约代码，如 IM2608")
    p_open.add_argument("entry_price", type=float, help="入场价")
    p_open.add_argument("entry_date", nargs="?", help="入场日期 YYYY-MM-DD（默认今天）")

    sub.add_parser("close", help="平仓")

    args = parser.parse_args()
    if args.cmd == "run":
        return cmd_run()
    if args.cmd == "status":
        return cmd_status()
    if args.cmd == "open":
        return cmd_open(args)
    if args.cmd == "close":
        return cmd_close(args)
    return 1


if __name__ == "__main__":
    sys.exit(main())
