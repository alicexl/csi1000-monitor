# monitor.py
from __future__ import annotations
import argparse
import sys
from pathlib import Path
from datetime import date, datetime

from config import Config, load_config, default_config_template
from db import (
    init_db, upsert_valuation, upsert_contract, insert_signal,
    query_latest_valuation, query_valuation_history,
    query_contracts_by_date, query_main_continuous_history,
    query_latest_signals,
)
from data_fetcher import fetch_valuation, fetch_main_continuous, fetch_daily_contracts
from valuation import compute_pct_for_windows, pe_pb_divergence
from signals import evaluate
from reporter import generate_report, render_status_line

ROOT = Path(__file__).parent
DEFAULT_DB = ROOT / "csi1000_monitor.db"
DEFAULT_CONFIG = ROOT / "config.yaml"
REPORTS_DIR = ROOT / "reports"


def _ensure_config(path: Path) -> Config:
    """配置文件不存在则从模板生成。"""
    if not path.exists():
        path.write_text(default_config_template(), encoding="utf-8")
        print(f"[INFO] 已生成默认配置: {path}", file=sys.stderr)
        print(f"[INFO] 请按需编辑后重新运行", file=sys.stderr)
    return load_config(path)


def cmd_scan(args) -> int:
    cfg = _ensure_config(Path(args.config))
    db_path = Path(args.db)
    conn = init_db(db_path)
    today = date.today()

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

    print("[2/3] 拉取主力连续 IM0...", flush=True)
    main_rows = fetch_main_continuous(spot_close, today)
    new_main = 0
    for r in main_rows:
        if upsert_contract(conn, r):
            new_main += 1
    print(f"      OK {len(main_rows)} 行，新增 {new_main}", flush=True)

    # 3. 拉当日 IM 合约
    print("[3/3] 拉取当日 IM 合约...", flush=True)
    contract_rows = fetch_daily_contracts(today, spot_close)
    new_ct = 0
    for r in contract_rows:
        if upsert_contract(conn, r):
            new_ct += 1
    print(f"      OK {len(contract_rows)} 合约，新增 {new_ct}", flush=True)

    conn.close()
    return 0


def _build_metrics(conn, cfg: Config) -> dict:
    """从 DB 拉数据，组装 metrics dict 供 signals/reporter 使用。"""
    latest = query_latest_valuation(conn)
    if latest is None:
        return {}
    history = query_valuation_history(conn, days=3650)
    contracts = query_contracts_by_date(conn, latest["date"])

    # 多区间分位
    pe_ttm_pct = compute_pct_for_windows(history, latest, "pe_ttm", cfg.pct_windows)
    pe_static_pct = compute_pct_for_windows(history, latest, "pe_static", cfg.pct_windows)
    pb_pct = compute_pct_for_windows(history, latest, "pb", cfg.pct_windows)

    pe_ttm = latest.get("pe_ttm") or 0
    pb = latest.get("pb") or 0
    close = latest.get("close") or 0

    # 主力连续贴水分位（近2年）
    main_hist = query_main_continuous_history(conn, days=730)
    main_basises = [abs(r["basis"]) for r in main_hist if r.get("basis") is not None]
    cur_main = main_hist[-1] if main_hist else None
    if cur_main and main_basises:
        cur_main_abs = abs(cur_main.get("basis", 0))
        main_pct = sum(1 for b in main_basises if b <= cur_main_abs) / len(main_basises) * 100
    else:
        main_pct = None

    return {
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
            pe_ttm_pct.get("10y", 0), pb_pct.get("10y", 0)),
        "contracts": contracts,
        "main_continuous_discount_pct": main_pct,
    }


def cmd_report(args) -> int:
    cfg = _ensure_config(Path(args.config))
    conn = init_db(Path(args.db))
    metrics = _build_metrics(conn, cfg)
    if not metrics:
        print("[ERR] DB 无数据，请先运行 scan", file=sys.stderr)
        return 1

    # 信号评估
    cur_month = next(
        (c for c in metrics["contracts"] if c["contract_type"] == "当月"), None)
    cur_month_disc = cur_month["annualized_discount"] if cur_month else 0
    cur_month_days = cur_month["days_to_expire"] if cur_month else 999

    signal_metrics = {
        "pe_ttm_pct_10y": metrics["pe_ttm_pct"].get("10y", 100),
        "current_month_discount": cur_month_disc,
        "current_month_days": cur_month_days,
    }
    sigs = evaluate(cfg.position.status, signal_metrics, cfg.thresholds)

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
    report = generate_report(metrics["date"], cfg, metrics, sigs)
    REPORTS_DIR.mkdir(exist_ok=True)
    out_path = REPORTS_DIR / f"csi1000_{metrics['date']}.md"
    out_path.write_text(report, encoding="utf-8")
    print(f"[OK] 报告已生成: {out_path}")

    conn.close()
    return 0


def cmd_status(args) -> int:
    cfg = _ensure_config(Path(args.config))
    conn = init_db(Path(args.db))
    metrics = _build_metrics(conn, cfg)
    if not metrics:
        print("[ERR] DB 无数据，请先运行 scan", file=sys.stderr)
        return 1

    cur_month = next(
        (c for c in metrics["contracts"] if c["contract_type"] == "当月"), None)
    cur_month_disc = cur_month["annualized_discount"] if cur_month else 0
    cur_month_days = cur_month["days_to_expire"] if cur_month else 999

    sigs = evaluate(cfg.position.status, {
        "pe_ttm_pct_10y": metrics["pe_ttm_pct"].get("10y", 100),
        "current_month_discount": cur_month_disc,
        "current_month_days": cur_month_days,
    }, cfg.thresholds)
    top = min(sigs, key=lambda s: s.priority) if sigs else None
    sig_type = top.type if top else "none"

    print(render_status_line(metrics["date"], cfg, metrics, sig_type))
    conn.close()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="中证1000 贴水策略监控")
    parser.add_argument("--db", default=str(DEFAULT_DB), help="SQLite DB 路径")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG), help="配置文件路径")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("scan", help="拉数据入库")
    sub.add_parser("report", help="生成 Markdown 报告")
    sub.add_parser("status", help="一行快速查状态")
    sub.add_parser("run", help="scan + report 一键")

    args = parser.parse_args()
    if args.cmd == "scan":
        return cmd_scan(args)
    elif args.cmd == "report":
        return cmd_report(args)
    elif args.cmd == "status":
        return cmd_status(args)
    elif args.cmd == "run":
        rc = cmd_scan(args)
        if rc != 0:
            return rc
        return cmd_report(args)
    return 1


if __name__ == "__main__":
    sys.exit(main())
