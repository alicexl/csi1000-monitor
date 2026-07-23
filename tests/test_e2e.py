# tests/test_e2e.py
"""E2E: 灌入 fixture 数据 → 跑 report → 校验报告关键内容。"""
from __future__ import annotations
import unittest
import tempfile
import os
from pathlib import Path

from signals import evaluate, Position, Thresholds
from db import init_db, upsert_valuation, upsert_contract
from monitor import compute_pct_for_windows, pe_pb_divergence
from reporter import generate_report, render_status_line


PCT_WINDOWS = ["10y", "5y", "all"]


def seed_fixture_db(db_path):
    """灌入 10 天模拟数据 + 4 个合约。"""
    conn = init_db(Path(db_path))
    # 10 天估值（模拟 PE 从 30 升到 35）
    for i, (d, close, pe_t, pb_v) in enumerate([
        ("2026-07-01", 8000, 30.0, 2.40),
        ("2026-07-02", 8050, 30.5, 2.42),
        ("2026-07-03", 8100, 31.0, 2.45),
        ("2026-07-04", 8150, 32.0, 2.48),
        ("2026-07-07", 8200, 33.0, 2.50),
        ("2026-07-08", 8117, 33.5, 2.55),
        ("2026-07-09", 8300, 34.0, 2.56),
        ("2026-07-10", 8198, 34.57, 2.58),
    ]):
        upsert_valuation(conn, {
            "date": d, "close": close,
            "pe_ttm": pe_t,
            "pb": pb_v,
            "fetched_at": "2026-07-12T10:00:00",
        })
    # 当日合约
    for sym, ctype, close, days, expire, basis, disc in [
        ("IM2607", "当月", 8150, 7, "2026-07-17", -48, 30.5),
        ("IM2608", "下月", 8098, 35, "2026-08-21", -100, 12.8),
        ("IM2609", "当季", 8020, 68, "2026-09-18", -178, 9.9),
        ("IM2612", "下季", 7850, 163, "2026-12-18", -348, 8.0),
    ]:
        upsert_contract(conn, {
            "date": "2026-07-10", "symbol": sym, "name": f"中证1000 {sym[-2:]}",
            "contract_type": ctype, "close": close, "settle": close - 5,
            "volume": 100000, "open_interest": 50000,
            "expire_date": expire, "days_to_expire": days,
            "basis": basis, "annualized_discount": disc,
            "fetched_at": "2026-07-12T10:00:00",
        })
    return conn


class TestE2EReport(unittest.TestCase):
    def setUp(self):
        self._fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(self._fd)
        self.conn = seed_fixture_db(self.db_path)

    def tearDown(self):
        self.conn.close()
        os.unlink(self.db_path)

    def _build_metrics(self):
        from db import (query_latest_valuation, query_valuation_history,
                        query_contracts_by_date)
        latest = query_latest_valuation(self.conn)
        history = query_valuation_history(self.conn)
        contracts = query_contracts_by_date(self.conn, latest["date"])
        pe_ttm_pct = compute_pct_for_windows(history, latest, "pe_ttm", PCT_WINDOWS)
        pb_pct = compute_pct_for_windows(history, latest, "pb", PCT_WINDOWS)
        return {
            "date": latest["date"], "close": latest["close"],
            "pe_ttm": latest["pe_ttm"],
            "pb": latest["pb"],
            "pe_ttm_pct": pe_ttm_pct, "pb_pct": pb_pct,
            "eps_ttm": latest["close"] / latest["pe_ttm"],
            "bps": latest["close"] / latest["pb"],
            "pe_pb_divergence": pe_pb_divergence(
                pe_ttm_pct.get("10y", {}).get("pct") or 0,
                pb_pct.get("10y", {}).get("pct") or 0),
            "contracts": contracts,
        }

    def test_empty_state_report(self):
        pos = Position(status="empty")
        t = Thresholds()
        metrics = self._build_metrics()
        cur_month = next(c for c in metrics["contracts"] if c["contract_type"] == "当月")
        next_month = next(c for c in metrics["contracts"] if c["contract_type"] == "下月")
        d_near = cur_month["annualized_discount"]
        d_far = next_month["annualized_discount"]
        # roll_yield = (当月价 − 下月价)/当月价（价格 back 判定，非年化贴水斜率）
        near_p, far_p = cur_month["close"], next_month["close"]
        roll = (near_p - far_p) / near_p * 100
        sigs = evaluate("empty", {
            "pe_ttm_pct_10y": metrics["pe_ttm_pct"]["10y"].get("pct") or 100,
            "current_month_discount": d_near,
            "current_month_days": cur_month["days_to_expire"],
            "next_month_discount": d_far,
            "roll_yield": roll,
        }, t)
        report = generate_report("2026-07-10", pos, metrics, sigs)
        # 关键内容存在
        self.assertIn("中证1000", report)
        self.assertIn("空仓", report)
        self.assertIn("PE_TTM", report)
        self.assertIn("IM2607", report)
        # 信号类型应包含 wait 或 entry 或 warn_entry 之一
        sig_types = {s.type for s in sigs}
        self.assertTrue(sig_types & {"wait", "entry", "warn_entry"})

    def test_holding_state_status_line(self):
        pos = Position(status="holding", contract="IM2607",
                       entry_date="2026-06-15", entry_price=7500.0)
        t = Thresholds()
        metrics = self._build_metrics()
        cur_month = next(c for c in metrics["contracts"] if c["contract_type"] == "当月")
        next_month = next(c for c in metrics["contracts"] if c["contract_type"] == "下月")
        d_near = cur_month["annualized_discount"]
        d_far = next_month["annualized_discount"]
        # roll_yield = (当月价 − 下月价)/当月价（价格 back 判定）
        near_p, far_p = cur_month["close"], next_month["close"]
        roll = (near_p - far_p) / near_p * 100
        sigs = evaluate("holding", {
            "pe_ttm_pct_10y": metrics["pe_ttm_pct"]["10y"].get("pct") or 100,
            "current_month_discount": d_near,
            "current_month_days": cur_month["days_to_expire"],
            "next_month_discount": d_far,
            "roll_yield": roll,
        }, t)
        top = min(sigs, key=lambda s: s.priority)
        line = render_status_line("2026-07-10", pos, metrics, top.type, roll)
        self.assertIn("持仓", line)
        self.assertIn("2026-07-10", line)
        self.assertIn("展期收益", line)


if __name__ == "__main__":
    unittest.main()
