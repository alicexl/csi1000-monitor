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
    query_contracts_by_date,
    load_position, save_position,
)
from data_fetcher import fetch_valuation, fetch_daily_contracts, fetch_otm_call
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


# ─── BPS 净资产趋势回归（附录方法论：净资产底缓慢抬升的证明）─────
# BPS = close / pb = 指数隐含每股净资产（Book value Per Share 的指数口径）。
# 对**全量 BPS 点**做对数回归 ln(BPS) = a + b*t，拟合"净资产增长趋势线"，
# 证明净资产长期复利增长、底部不归零（中证1000 有基本面支撑）。
#
# 用 BPS 而非纯点位：BPS 剥离了 PE/PB 周期波动，只保留净资产复利增长，
# 对数回归 R²≈0.90（vs 纯点位 0.46），规律性远优于点位。
#
# 注：BPS 偏离趋势线反映**盈利周期**（盈利/减值/调样）而非估值便宜/贵，
# 故此回归仅作附录方法论证明，**不进任何信号**（见 README §七/§8.5）。
# 另加线性回归 BPS = a + b*t：净资产近似"每年加固定点数"（+105 点/年，线性
# R²≈0.895），与对数（年化增长率 %）互补——一个看绝对点数、一个看增长率。
FIT_MIN_POINTS = 3      # 回归最少点数，不足返回 None（对数 + 线性共用）
BOTTOM_START_DATE = "2014-10-17"  # PB 数据起点（指数上市首日）；此前无官方 PB

# PB 压缩空间情景：固定资产 B（=当前 BPS），看不同 PB 分位下的点位与跌幅。
# 用历史 PB 经验分位定义情景，分位刻度借正态 σ 的概率语言标注"出现难易程度"
# （经验分位本身即为出现概率，σ 仅作直观标签，不假设 PB 正态——PB 右偏严重，
# 用均值±σ 理论值会穿铁底，故取经验分位对应的 PB 实际值）：
#   50%(0σ) 中位 ｜ 15.9%(-1σ) 低估 ｜ 2.3%(-2σ) 极低估（历史仅 2.3% 时间更低）
# 跨周期可比，避免固定值（如 1.5）随时间失效。只取左尾低估侧，右尾(2015 极值)无参考价值。
PB_COMPRESSION_PERCENTILES = [50, 15.9, 2.3]


def compute_bps(close: float, pb: float) -> float:
    """BPS = 指数点位 / 市净率 ≈ 隐含每股净资产。pb<=0 返回 0（无意义）。"""
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


# 经验分位(%) → 正态 σ 标签（借概率语言标"出现难易"，不假设分布正态）
_PCT_TO_SIGMA = {50: "0σ", 15.9: "-1σ", 2.3: "-2σ", 84.1: "+1σ", 97.7: "+2σ"}


def pb_compression_scenarios(
    current_close: float, current_pb: float,
    pb_history: list[float] | None = None,
    pcts: list[float] = PB_COMPRESSION_PERCENTILES,
) -> list[dict] | None:
    """PB 压缩空间：固定资产 B=close/pb，算各 PB 分位情景对应点位与跌幅。

    逻辑：P = B × PB，B 固定（当前净资产），PB 越低 → P 越低。
    情景用历史 PB 经验分位定义（50%中位/15.9%低估/2.3%极低估），跨周期可比。
    分位刻度借正态 σ 概率标签：15.9%=历史仅 15.9% 时间更低(-1σ)，2.3%=-2σ。
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
            sig = _PCT_TO_SIGMA.get(pct)
            tag = f"PB {pct}%分位 ({sig})" if sig else f"PB {pct}%分位"
            rows.append({"pb": pb, "price": price, "drop_pct": drop, "tag": tag})
    return rows


def fit_bps_log(
    points: list[tuple[str, float]],
) -> dict | None:
    """对**全量 BPS 点**做对数回归 ln(BPS) = a + b*date_ordinal，拟合净资产增长趋势。

    返回 {a, b, r2, annual_pct, trend_now, n} 或 None（有效点不足）。
    - annual_pct: 年化增长率 = b*365*100（对数回归下即净资产连续复利年化增速）
    - trend_now: 当前日期的趋势线值 = exp(a + b*t_today)
    - 与 fit_bps_linear（绝对点数/年）互补：这看年化增长率 %。
    """
    xs, ys = [], []
    for d, v in points:
        if v <= 0:
            continue
        xs.append(datetime.strptime(d[:10], "%Y-%m-%d").toordinal())
        ys.append(math.log(v))
    if len(xs) < FIT_MIN_POINTS:
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
    }


def fit_bps_linear(points: list[tuple[str, float]]) -> dict | None:
    """对全量 BPS 做线性回归 BPS = a + b*t（t=距首日年数）。

    与 fit_bps_log（对数回归、年化增长率 %）互补：那看增长率，这看绝对点数。
    中证1000 近 12 年近似"每年 +105 点"线性增长（R²≈0.895），是净资产趋势的
    直观口径。

    返回 {slope_pt_per_year, r2, intercept, n} 或 None（点不足）。
    - slope_pt_per_year: 每年净加点数（线性斜率 b）
    - intercept: 截距 a（t=0 即首日）；r2: 线性决定系数
    """
    pts = [(d, v) for d, v in points if v > 0]
    if len(pts) < FIT_MIN_POINTS:
        return None
    first = datetime.strptime(pts[0][0][:10], "%Y-%m-%d")
    xs, ys = [], []
    for d, v in pts:
        t = (datetime.strptime(d[:10], "%Y-%m-%d") - first).days / 365.25
        xs.append(t)
        ys.append(v)
    n = len(xs)
    xm, ym = sum(xs) / n, sum(ys) / n
    sxx = sum((x - xm) ** 2 for x in xs)
    sxy = sum((x - xm) * (y - ym) for x, y in zip(xs, ys))
    syy = sum((y - ym) ** 2 for y in ys)
    b = sxy / sxx if sxx else 0.0
    a = ym - b * xm
    r2 = (sxy * sxy) / (sxx * syy) if (sxx and syy) else 0.0
    return {"slope_pt_per_year": b, "r2": r2, "intercept": a, "n": n}


def plot_bps_trend(
    points: list[tuple[str, float]], current_bps: float, current_date: str,
) -> str | None:
    """画 BPS 散点 + 线性/对数拟合线，返回 base64 内联 PNG（不落盘）。

    嵌入 Markdown 报告用 `<img src="data:image/png;base64,...">`。matplotlib 用
    Agg backend（无显示环境）；未装 matplotlib 或点不足返回 None，报告降级无图。

    画的是**全量点**两条回归（线性 + 对数），与 fit_bps_log / fit_bps_linear
    同口径——图与表一致，都对全量 BPS 回归。
    """
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import matplotlib.dates as mdates
        import base64
        import io
    except ImportError:
        return None

    pts = [(d, v) for d, v in points if v > 0]
    if len(pts) < 3:
        return None
    first = date.fromisoformat(pts[0][0][:10])
    ds, ts, bs = [], [], []
    for d, v in pts:
        dt = date.fromisoformat(d[:10])
        ds.append(dt)
        ts.append((dt - first).days / 365.25)
        bs.append(v)

    # 线性 + 对数回归（全量点，闭式最小二乘）
    n = len(ts)
    xm, ym = sum(ts) / n, sum(bs) / n
    sxx = sum((x - xm) ** 2 for x in ts)
    sxy = sum((x - xm) * (y - ym) for x, y in zip(ts, bs))
    syy = sum((y - ym) ** 2 for y in bs)
    b1 = sxy / sxx if sxx else 0.0
    a1 = ym - b1 * xm
    r2_1 = (sxy * sxy) / (sxx * syy) if (sxx and syy) else 0.0
    ys2 = [math.log(y) for y in bs]
    ym2 = sum(ys2) / n
    sxy2 = sum((x - xm) * (y - ym2) for x, y in zip(ts, ys2))
    syy2 = sum((y - ym2) ** 2 for y in ys2)
    b2 = sxy2 / sxx if sxx else 0.0
    a2 = ym2 - b2 * xm
    r2_2 = (sxy2 * sxy2) / (sxx * syy2) if (sxx and syy2) else 0.0
    cagr = math.exp(b2) - 1

    plt.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei", "DejaVu Sans"]
    plt.rcParams["axes.unicode_minus"] = False
    fig, ax = plt.subplots(figsize=(11, 5.5))
    ax.scatter(ds, bs, s=5, c=ts, cmap="viridis", alpha=0.45,
               label=f"每日 BPS（n={n}）")
    grid_t = [ts[0] + (ts[-1] - ts[0]) * i / 200 for i in range(201)]
    grid_d = [first + timedelta(days=t * 365.25) for t in grid_t]
    ax.plot(grid_d, [a1 + b1 * t for t in grid_t], color="#d62728", lw=2,
            label=f"线性 +{b1:.0f}点/年  R²={r2_1:.3f}")
    ax.plot(grid_d, [math.exp(a2 + b2 * t) for t in grid_t],
            color="#1f77b4", lw=2, ls="--",
            label=f"对数 CAGR {cagr * 100:.2f}%/年  R²={r2_2:.3f}")
    ax.scatter([ds[-1]], [current_bps], color="red", s=45, zorder=5,
               edgecolor="white")
    ax.annotate(f"当前 {current_bps:.0f}\n({current_date})",
                xy=(ds[-1], current_bps), xytext=(-90, 25),
                textcoords="offset points", fontsize=9,
                arrowprops=dict(arrowstyle="->", color="red"), color="red")
    ax.set_title("中证1000 BPS 长期趋势", fontsize=13, pad=10)
    ax.set_xlabel("日期")
    ax.set_ylabel("BPS（指数点）")
    ax.legend(loc="upper left", fontsize=9, framealpha=0.9)
    ax.grid(True, alpha=0.3)
    ax.xaxis.set_major_locator(mdates.YearLocator())
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y"))
    fig.autofmt_xdate()
    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=110, bbox_inches="tight")
    plt.close(fig)
    return base64.b64encode(buf.getvalue()).decode("ascii")


# 预期收益计算的默认假设（来自杨康平《股指期货吃贴水策略》PDF 经验值）
DEFAULT_DIVIDEND_YIELD = 1.0  # 中证1000 近年股息率约 1-2%，取保守下限


def _window_median(history: list[dict], field: str, days: int) -> float | None:
    """指定天数窗口内某字段的中位数（复用 _filter_by_window，cutoff 口径一致）。"""
    values = _filter_by_window(history, field, days)
    if not values:
        return None
    values.sort()
    n = len(values)
    mid = n // 2
    return values[mid] if n % 2 == 1 else (values[mid - 1] + values[mid]) / 2


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

    **展期收益不计入此 panel**：吃贴水策略的核心收益来自价格 backwardation（roll_yield =
    展期一次收益率 = (当月价 − 下月价)/当月价），但该值随期限结构变化、难以多年预测。
    用户可参考期货合约表的基差/年化贴水直观判断当前曲线健康度。status_line 一行单独展示 roll_yield。

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

    策略判断基于**展期收益 roll_yield = 展期一次收益率 = (当月价 − 下月价)/当月价**：
    roll_yield > 0 表示价格 backwardation（下月比当月便宜），展期（卖近月买远月）能吃到价差。
    用绝对价格判定，不用年化贴水斜率——远月天数长，即使年化贴水略低，绝对价格通常仍更低
    （back），照样吃贴水；只有价格真正 contango（下月更贵）才失效。
    当月/下月年化贴水保留作展示参考（status_line/报告期货合约表）。
    days 用于 switch 信号（当月临交割时切月）。
    """
    contracts = metrics.get("contracts", [])
    cur_month = next(
        (c for c in contracts if c["contract_type"] == "当月"), None)
    next_month = next(
        (c for c in contracts if c["contract_type"] == "下月"), None)
    d_near = cur_month["annualized_discount"] if cur_month else 0
    d_far = next_month["annualized_discount"] if next_month else 0
    # roll_yield = 展期一次收益率（价格 back 判定）：当月价 − 下月价 > 0 即 backwardation
    near_price = cur_month["close"] if cur_month else 0
    far_price = next_month["close"] if next_month else 0
    if cur_month and next_month and near_price > 0:
        roll_yield = (near_price - far_price) / near_price * 100
    else:
        roll_yield = 0.0  # 缺当月/下月 → 无法展期，判 ≤0 异常
    return {
        "pe_ttm_pct_10y": metrics["pe_ttm_pct"].get("10y", {}).get("pct")
                          or 100,  # 样本不足（None）→ 100，保守 wait 不入场
        "pb_pct_10y": metrics.get("pb_pct", {}).get("10y", {}).get("pct"),  # None → 不入场
        "current_month_discount": d_near,
        "current_month_days": cur_month["days_to_expire"] if cur_month else 999,
        "next_month_discount": d_far,
        "roll_yield": roll_yield,
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
    print("[1/2] 拉取 PE/PB 历史...", flush=True)
    val_rows = fetch_valuation()
    val_ins = val_upd = 0
    for r in val_rows:
        if upsert_valuation(conn, r) == "inserted":
            val_ins += 1
        else:
            val_upd += 1
    print(f"      OK {len(val_rows)} 行（新增 {val_ins}，更新 {val_upd}）", flush=True)

    # 2. 入库当日 IM 合约（需最新现货收盘算基差）
    latest = query_latest_valuation(conn)
    if latest is None:
        print("[ERR] 无估值数据，无法拉期货", file=sys.stderr)
        return 1
    spot_close = latest["close"]

    today, cached_contracts = _resolve_trade_date(spot_close)
    if today != date.today():
        print(f"[INFO] 今日 CFFEX 数据未发布，使用 {today}", file=sys.stderr)

    print(f"[2/2] 入库 IM 合约（{today}）...", flush=True)
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
    """从估值历史构造全量 BPS 序列 → 对数回归 + 线性回归 → 补当前 BPS 指标。

    只用 BOTTOM_START_DATE（2014-10-17）之后的数据——此前无官方 PB。
    返回 fit_bps_log 结果 + 当前 BPS + PB 分位情景点位 + 线性拟合 + 趋势图，或 None。
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
        points.append((str(d)[:10], compute_bps(c, pb)))
        pb_history.append(pb)
    if len(points) < FIT_MIN_POINTS:
        return None

    fit = fit_bps_log(points)
    if fit is None:
        return None

    cur_bps = compute_bps(current_close, current_pb)
    fit["current_bps"] = cur_bps

    # PB 压缩空间：固定资产 B，各 PB 情景对应点位与跌幅
    fit["pb_compression"] = pb_compression_scenarios(
        current_close, current_pb, pb_history)

    # 线性拟合（全量点，看净资产整体每年加多少点；与对数增长率互补）
    fit["linear"] = fit_bps_linear(points)

    # BPS 趋势图（base64 内联 PNG，不落盘；matplotlib 缺失/点不足返回 None）
    # 当前日期取最新估值行的 date（points 按日期升序，末行即最新）
    cur_date_str = points[-1][0] if points else ""
    fit["bps_trend_png"] = plot_bps_trend(points, cur_bps, cur_date_str)
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
    bottom_trend: dict | None,
    t: Thresholds = THRESHOLDS,
) -> dict | None:
    """组装 IM Carry Score。三因子缺任一（贴水 / PB 分位 / -1σ 跌幅）返回 None。

    第三因子 = 1 年贴水覆盖 -1σ：coverage_ratio = 下季贴水年化 / |PB -1σ 跌幅|。
    ≥1 表示持有 1 年的贴水能填平一次 PB 常态杀跌（-1σ），下行有保护。
    """
    discount = _next_quarter_discount(contracts)
    if discount is None or pb_pct_10y is None or not bottom_trend:
        return None
    sigma1 = next((r["drop_pct"] for r in bottom_trend.get("pb_compression") or []
                   if "(-1σ)" in r.get("tag", "")), None)
    # -1σ 跌幅缺失或为 0（无历史 PB 波动）→ 无法算覆盖比
    if sigma1 is None or sigma1 == 0:
        return None
    coverage_ratio = discount / abs(sigma1)
    cs = score_carry(discount, pb_pct_10y, coverage_ratio, t)
    return {
        "total": cs.total,
        "discount_pts": cs.discount_pts,
        "pb_pts": cs.pb_pts,
        "coverage_pts": cs.coverage_pts,
        "discount_value": cs.discount_value,
        "pb_pct": cs.pb_pct,
        "coverage_ratio": cs.coverage_ratio,
        "band": cs.band,
    }


def _compute_discount_coverage(
    contracts: list[dict], bottom_trend: dict | None,
    years: tuple = (1,),
) -> dict | None:
    """下季贴水年化 × 1 年 vs PB -1σ/-2σ 跌幅，判断能否覆盖。

    辅助决策视角：持有吃贴水 1 年的累计收益，能否填平一次 PB 杀跌。
    贴水只看 1 年——更长 horizon 是贴水均值回归的外推，不可靠；3 年才覆盖说明
    下行保护不足，不值得入场（见 Carry Score 覆盖因子）。
    - 展期收益按线性累计（保守口径，不复利）：累计 = 年化 × 年数
    - 跌幅取 PB 压缩情景的 -1σ（主判）/ -2σ（极端参考），drop_pct 负值
    - 累计贴水 + drop_pct ≥ 0 → 已覆盖，否则未覆盖
    任一输入缺失返回 None。
    """
    discount = _next_quarter_discount(contracts)
    if discount is None or not bottom_trend:
        return None
    pb_rows = bottom_trend.get("pb_compression") or []
    scenarios = []
    for r in pb_rows:
        tag = r.get("tag", "")
        if "(-1σ)" in tag:        # 主判：常态杀跌，贴水应能覆盖
            scenarios.append({"label": "-1σ", "drop_pct": r["drop_pct"]})
        elif "(-2σ)" in tag:      # 极端参考：黑天鹅级，不要求覆盖
            scenarios.append({"label": "-2σ", "drop_pct": r["drop_pct"]})
    if not scenarios:
        return None
    return {
        "discount_annual": discount,
        "years": list(years),
        "scenarios": scenarios,
    }


def _build_metrics(conn) -> dict:
    """从 DB 拉数据，组装 metrics dict 供 signals/reporter 使用。"""
    latest = query_latest_valuation(conn)
    if latest is None:
        return {}
    history = query_valuation_history(conn)
    contracts = query_contracts_by_date(conn, latest["date"])

    # 多区间分位
    pe_ttm_pct = compute_pct_for_windows(history, latest, "pe_ttm", PCT_WINDOWS)
    pb_pct = compute_pct_for_windows(history, latest, "pb", PCT_WINDOWS)

    pe_ttm = latest.get("pe_ttm") or 0
    pb = latest.get("pb") or 0
    close = latest.get("close") or 0

    # 10 年窗口 PE_TTM 中位数（用于估值回归预期）
    pe_median_10y = _window_median(history, "pe_ttm", WINDOW_DAYS["10y"])

    # BPS 净资产趋势回归：close/pb ≈ 隐含净资产，对全量 BPS 对数回归拟合增长趋势（附录方法论）
    bottom_trend = _compute_bottom_trend(history, close, pb)

    metrics = {
        "date": latest["date"],
        "close": close,
        "pe_ttm": pe_ttm,
        "pb": pb,
        "pe_ttm_pct": pe_ttm_pct,
        "pb_pct": pb_pct,
        "eps_ttm": close / pe_ttm if pe_ttm else 0,
        "bps": compute_bps(close, pb),
        "pe_pb_divergence": pe_pb_divergence(
            pe_ttm_pct.get("10y", {}).get("pct") or 0,
            pb_pct.get("10y", {}).get("pct") or 0),
        "contracts": contracts,
        "bottom_trend": bottom_trend,
    }

    # IM Carry Score（下季贴水 + PB 分位 + 1年贴水覆盖-1σ，满分 100）
    metrics["carry_score"] = _compute_carry_score(
        contracts,
        pb_pct.get("10y", {}).get("pct"),
        bottom_trend,
    )

    # 贴水覆盖性（下季贴水年化 × N 年 vs PB 杀跌跌幅，辅助决策）
    metrics["discount_coverage"] = _compute_discount_coverage(contracts, bottom_trend)

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

    # 展期收益 roll_yield（= 展期一次收益率，价格是否 back）供开仓检查面板共用
    metrics["roll_yield"] = _extract_signal_metrics(metrics)["roll_yield"]
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
