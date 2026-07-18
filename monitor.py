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
# 各窗口预期样本数（A 股每年约 244 交易日）；all 不设预期（全历史即全样本）
EXPECTED_SAMPLES = {"10y": 2440, "5y": 1220, "all": None}
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
    """算多区间分位。返回 {window_name: {pct, n, expected}}。

    - pct: 分位值；样本不足（n < MIN_SAMPLES）时为 None
    - n: 实际样本数
    - expected: 预期样本数（A 股 244/年）；all 窗口为 None
    """
    current_val = current.get(field)
    if current_val is None:
        return {w: {"pct": None, "n": 0, "expected": EXPECTED_SAMPLES.get(w)}
                for w in windows}
    current_val = float(current_val)
    result = {}
    for w in windows:
        days = WINDOW_DAYS.get(w, 99999)
        series = _filter_by_window(history, field, days)
        n = len(series)
        pct = percentile(series, current_val) if n >= MIN_SAMPLES else None
        result[w] = {"pct": pct, "n": n, "expected": EXPECTED_SAMPLES.get(w)}
    return result


def pe_pb_divergence(pe_pct: float, pb_pct: float) -> float:
    """PE 分位 - PB 分位。正值=盈利低位，负值=净资产膨胀。"""
    return pe_pct - pb_pct


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


def _extract_signal_metrics(metrics: dict, switch_days: int = 7) -> dict:
    """从 metrics 抽出 signals.evaluate 需要的指标 dict。

    当月临近交割（剩余天数 < switch_days，含已交割 days=0）时，
    信号判定 fallback 到下月合约 —— 和 switch_signal 持仓侧切换规则对齐，
    也保证入场信号参考的是"实际可交易"的近月合约。
    """
    contracts = metrics.get("contracts", [])
    cur_month = next(
        (c for c in contracts if c["contract_type"] == "当月"), None)
    next_month = next(
        (c for c in contracts if c["contract_type"] == "下月"), None)
    if cur_month and cur_month.get("days_to_expire", 999) < switch_days and next_month:
        active = next_month
    else:
        active = cur_month
    return {
        "pe_ttm_pct_10y": metrics["pe_ttm_pct"].get("10y", {}).get("pct")
                          or 100,  # 样本不足（None）→ 100，保守 wait 不入场
        "current_month_discount": active["annualized_discount"] if active else 0,
        "current_month_days": active["days_to_expire"] if active else 999,
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
    new_val = 0
    for r in val_rows:
        if upsert_valuation(conn, r):
            new_val += 1
    print(f"      OK {len(val_rows)} 行，新增 {new_val}", flush=True)

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
    main_rows = fetch_main_continuous(spot_close, today)
    new_main = 0
    for r in main_rows:
        if upsert_contract(conn, r):
            new_main += 1
    print(f"      OK {len(main_rows)} 行，新增 {new_main}", flush=True)

    # 3. 入库当日 IM 合约（复用 _resolve_trade_date 的结果）
    print(f"[3/3] 入库 IM 合约（{today}）...", flush=True)
    new_ct = 0
    for r in cached_contracts:
        if upsert_contract(conn, r):
            new_ct += 1
    print(f"      OK {len(cached_contracts)} 合约，新增 {new_ct}", flush=True)

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

    sigs = evaluate(position.status, _extract_signal_metrics(metrics, THRESHOLDS.switch_days), THRESHOLDS)

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

    sig_metrics = _extract_signal_metrics(metrics, THRESHOLDS.switch_days)
    sigs = evaluate(position.status, sig_metrics, THRESHOLDS)
    top = min(sigs, key=lambda s: s.priority) if sigs else None
    sig_type = top.type if top else "none"

    print(render_status_line(metrics["date"], position, metrics, sig_type,
                             sig_metrics["current_month_discount"]))
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
