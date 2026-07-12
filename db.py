# db.py
from __future__ import annotations
import sqlite3
import threading
from pathlib import Path
from datetime import datetime, timedelta
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS daily_valuation (
    date          TEXT PRIMARY KEY,
    close         REAL NOT NULL,
    pe_static     REAL,
    pe_ttm        REAL,
    pe_ttm_eq     REAL,
    pe_static_med REAL,
    pe_ttm_med    REAL,
    pb            REAL,
    pb_med        REAL,
    pb_w          REAL,
    fetched_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS daily_contracts (
    date                TEXT NOT NULL,
    symbol              TEXT NOT NULL,
    name                TEXT,
    contract_type       TEXT,
    close               REAL,
    settle              REAL,
    volume              REAL,
    open_interest       REAL,
    expire_date         TEXT,
    days_to_expire      INTEGER,
    basis               REAL,
    annualized_discount REAL,
    fetched_at          TEXT NOT NULL,
    PRIMARY KEY (date, symbol)
);

CREATE TABLE IF NOT EXISTS signals (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    date          TEXT NOT NULL,
    signal_type   TEXT NOT NULL,
    condition     TEXT,
    current_value TEXT,
    threshold     TEXT,
    suggestion    TEXT,
    created_at    TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_valuation_date ON daily_valuation(date);
CREATE INDEX IF NOT EXISTS idx_contracts_date ON daily_contracts(date);
CREATE INDEX IF NOT EXISTS idx_signals_date ON signals(date);
"""

_init_lock = threading.Lock()


def init_db(db_path: Path) -> sqlite3.Connection:
    """创建连接 + 初始化 schema（线程安全）。
    使用直接连接（非 thread-local），因为 scan/report 单线程运行。
    """
    conn = sqlite3.connect(str(db_path), timeout=5.0)
    conn.row_factory = sqlite3.Row
    with _init_lock:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.executescript(SCHEMA)
        conn.commit()
    return conn


# Alias for compatibility (test imports get_conn)
get_conn = init_db


def upsert_valuation(conn: sqlite3.Connection, row: dict[str, Any]) -> bool:
    """插入估值行；PK 冲突则忽略。返回 True=新增, False=已存在。"""
    sql = """
    INSERT OR IGNORE INTO daily_valuation
        (date, close, pe_static, pe_ttm, pe_ttm_eq, pe_static_med,
         pe_ttm_med, pb, pb_med, pb_w, fetched_at)
    VALUES (:date, :close, :pe_static, :pe_ttm, :pe_ttm_eq, :pe_static_med,
            :pe_ttm_med, :pb, :pb_med, :pb_w, :fetched_at)
    """
    cur = conn.execute(sql, row)
    conn.commit()
    return cur.rowcount > 0


def upsert_contract(conn: sqlite3.Connection, row: dict[str, Any]) -> bool:
    sql = """
    INSERT OR IGNORE INTO daily_contracts
        (date, symbol, name, contract_type, close, settle, volume,
         open_interest, expire_date, days_to_expire, basis,
         annualized_discount, fetched_at)
    VALUES (:date, :symbol, :name, :contract_type, :close, :settle, :volume,
            :open_interest, :expire_date, :days_to_expire, :basis,
            :annualized_discount, :fetched_at)
    """
    cur = conn.execute(sql, row)
    conn.commit()
    return cur.rowcount > 0


def insert_signal(conn: sqlite3.Connection, signal: dict[str, Any]) -> int:
    sql = """
    INSERT INTO signals (date, signal_type, condition, current_value,
                         threshold, suggestion, created_at)
    VALUES (:date, :signal_type, :condition, :current_value,
            :threshold, :suggestion, :created_at)
    """
    cur = conn.execute(sql, signal)
    conn.commit()
    return cur.lastrowid


def query_latest_valuation(conn: sqlite3.Connection) -> dict[str, Any] | None:
    cur = conn.execute(
        "SELECT * FROM daily_valuation ORDER BY date DESC LIMIT 1")
    row = cur.fetchone()
    return dict(row) if row else None


def query_valuation_history(conn: sqlite3.Connection, days: int = 3650) -> list[dict[str, Any]]:
    cur = conn.execute(
        "SELECT * FROM daily_valuation ORDER BY date DESC LIMIT ?", (days,))
    rows = cur.fetchall()
    return [dict(r) for r in reversed(rows)]  # 按日期升序返回


def query_contracts_by_date(conn: sqlite3.Connection, date: str) -> list[dict[str, Any]]:
    cur = conn.execute(
        "SELECT * FROM daily_contracts WHERE date = ? AND symbol != 'IM0' "
        "ORDER BY symbol", (date,))
    return [dict(r) for r in cur.fetchall()]


def query_main_continuous_history(conn: sqlite3.Connection, days: int = 500) -> list[dict[str, Any]]:
    cur = conn.execute(
        "SELECT * FROM daily_contracts WHERE symbol = 'IM0' "
        "ORDER BY date DESC LIMIT ?", (days,))
    rows = cur.fetchall()
    return [dict(r) for r in reversed(rows)]


def query_latest_signals(conn: sqlite3.Connection, days: int = 30) -> list[dict[str, Any]]:
    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    cur = conn.execute(
        "SELECT * FROM signals WHERE date >= ? ORDER BY date DESC", (cutoff,))
    return [dict(r) for r in cur.fetchall()]
