# monitor.py
from __future__ import annotations
import argparse
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
from signals import evaluate, Thresholds, Position
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


def _build_metrics(conn) -> dict:
    """从 DB 拉数据，组装 metrics dict 供 signals/reporter 使用。"""
    latest = query_latest_valuation(conn)
    if latest is None:
        return {}
    history = query_valuation_history(conn, days=3650)
    contracts = query_contracts_by_date(conn, latest["date"])

    # 多区间分位
    pe_ttm_pct = compute_pct_for_windows(history, latest, "pe_ttm", PCT_WINDOWS)
    pe_static_pct = compute_pct_for_windows(history, latest, "pe_static", PCT_WINDOWS)
    pb_pct = compute_pct_for_windows(history, latest, "pb", PCT_WINDOWS)

    pe_ttm = latest.get("pe_ttm") or 0
    pb = latest.get("pb") or 0
    close = latest.get("close") or 0

    # 10 年窗口 PE_TTM 中位数（用于估值回归预期）
    pe_median_10y = _window_median(history, "pe_ttm", WINDOW_DAYS["10y"])

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
        "pe_static_pct": pe_static_pct,
        "pb_pct": pb_pct,
        "eps_ttm": close / pe_ttm if pe_ttm else 0,
        "bps": close / pb if pb else 0,
        "pe_pb_divergence": pe_pb_divergence(
            pe_ttm_pct.get("10y", {}).get("pct") or 0,
            pb_pct.get("10y", {}).get("pct") or 0),
        "contracts": contracts,
        "main_continuous_discount_pct": main_pct,
    }

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
