# db.py
from __future__ import annotations
import sqlite3
import threading
from pathlib import Path
from datetime import datetime, timedelta
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS daily_valuation (
    date        TEXT PRIMARY KEY,
    close       REAL NOT NULL,
    pe_static   REAL,
    pe_ttm      REAL,
    pb          REAL,
    fetched_at  TEXT NOT NULL
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
    created_at    TEXT NOT NULL,
    UNIQUE(date, signal_type, condition)
);

CREATE TABLE IF NOT EXISTS position (
    id          INTEGER PRIMARY KEY CHECK (id = 1),
    status      TEXT NOT NULL,
    contract    TEXT,
    entry_date  TEXT,
    entry_price REAL,
    updated_at  TEXT NOT NULL
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
        _migrate_signals_unique(conn)
        _migrate_main_continuous_basis(conn)
        conn.commit()
    return conn


def _migrate_main_continuous_basis(conn: sqlite3.Connection) -> None:
    """一次性修复老 DB 的主力连续 IM0 基差。

    旧 fetch_main_continuous 用'今日现货'算所有历史行的 basis，导致历史 |basis|
    偏大、今日 |basis| 几乎永远最小 → 主力贴水分位总是 ~5%。修复：用每行
    date 对应的现货收盘重算。user_version=1 标记已迁移，避免重复执行。
    """
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version >= 1:
        return
    conn.execute(
        "UPDATE daily_contracts SET basis = close - ("
        "  SELECT close FROM daily_valuation WHERE date = daily_contracts.date"
        ") WHERE symbol = 'IM0' AND basis IS NOT NULL "
        "  AND EXISTS (SELECT 1 FROM daily_valuation WHERE date = daily_contracts.date)"
    )
    conn.execute("PRAGMA user_version = 1")


def _migrate_signals_unique(conn: sqlite3.Connection) -> None:
    """老 DB signals 表无 UNIQUE(date, signal_type, condition) 约束 →
    清理重复行（按分组保留最小 id）+ 建唯一索引。新 DB 已在 schema 里声明 UNIQUE，跳过。
    """
    has_unique = conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='index' AND name='sqlite_autoindex_signals'"
    ).fetchone()[0]
    # 新 schema 里 UNIQUE 自动生成 sqlite_autoindex_signals；老 schema 没有
    has_unique_explicit = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='signals'"
    ).fetchone()
    sql_def = has_unique_explicit[0] if has_unique_explicit else ""
    if "UNIQUE(date, signal_type, condition)" in sql_def:
        return  # 新 schema
    # 老 schema：清理重复 + 加唯一索引
    conn.execute(
        "DELETE FROM signals WHERE id NOT IN ("
        "  SELECT MIN(id) FROM signals GROUP BY date, signal_type, condition"
        ")"
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_signals_dedup "
        "ON signals(date, signal_type, condition)"
    )


def upsert_valuation(conn: sqlite3.Connection, row: dict[str, Any]) -> str:
    """UPSERT：PK(date) 冲突则更新所有数值字段。返回 'inserted' 或 'updated'。

    数据源偶尔会修正历史数据（PE/PB 重算、合约盘后修正），INSERT OR IGNORE
    会丢失修正——这里用 ON CONFLICT UPDATE 保证最新值覆盖。
    """
    existed = conn.execute(
        "SELECT 1 FROM daily_valuation WHERE date = ?", (row["date"],)
    ).fetchone() is not None
    sql = """
    INSERT INTO daily_valuation
        (date, close, pe_static, pe_ttm, pb, fetched_at)
    VALUES (:date, :close, :pe_static, :pe_ttm, :pb, :fetched_at)
    ON CONFLICT(date) DO UPDATE SET
        close=excluded.close,
        pe_static=excluded.pe_static,
        pe_ttm=excluded.pe_ttm,
        pb=excluded.pb,
        fetched_at=excluded.fetched_at
    """
    conn.execute(sql, row)
    conn.commit()
    return "updated" if existed else "inserted"


def upsert_contract(conn: sqlite3.Connection, row: dict[str, Any]) -> str:
    """UPSERT：PK(date, symbol) 冲突则更新所有数值字段。返回 'inserted' 或 'updated'。"""
    existed = conn.execute(
        "SELECT 1 FROM daily_contracts WHERE date = ? AND symbol = ?",
        (row["date"], row["symbol"]),
    ).fetchone() is not None
    sql = """
    INSERT INTO daily_contracts
        (date, symbol, name, contract_type, close, settle, volume,
         open_interest, expire_date, days_to_expire, basis,
         annualized_discount, fetched_at)
    VALUES (:date, :symbol, :name, :contract_type, :close, :settle, :volume,
            :open_interest, :expire_date, :days_to_expire, :basis,
            :annualized_discount, :fetched_at)
    ON CONFLICT(date, symbol) DO UPDATE SET
        name=excluded.name,
        contract_type=excluded.contract_type,
        close=excluded.close,
        settle=excluded.settle,
        volume=excluded.volume,
        open_interest=excluded.open_interest,
        expire_date=excluded.expire_date,
        days_to_expire=excluded.days_to_expire,
        basis=excluded.basis,
        annualized_discount=excluded.annualized_discount,
        fetched_at=excluded.fetched_at
    """
    conn.execute(sql, row)
    conn.commit()
    return "updated" if existed else "inserted"


def insert_signal(conn: sqlite3.Connection, signal: dict[str, Any]) -> int:
    """插入信号；UNIQUE(date, signal_type, condition) 冲突则忽略。
    返回 lastrowid（0 或负数表示未插入，命中冲突时 SQLite 返回 0）。
    """
    sql = """
    INSERT OR IGNORE INTO signals (date, signal_type, condition, current_value,
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
    """返回历史估值行（按日期升序）。days 是行数上限，非天数；
    估值分位的天数窗口由 valuation.compute_pct_for_windows 自行过滤。
    """
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


def load_position(conn: sqlite3.Connection) -> dict[str, Any] | None:
    """读 position 表的单行记录。空表返回 None。"""
    cur = conn.execute("SELECT status, contract, entry_date, entry_price "
                       "FROM position WHERE id = 1")
    row = cur.fetchone()
    return dict(row) if row else None


def save_position(conn: sqlite3.Connection, row: dict[str, Any]) -> None:
    """INSERT OR REPLACE id=1。row 必须含 status/contract/entry_date/entry_price + updated_at。"""
    conn.execute(
        "INSERT OR REPLACE INTO position "
        "(id, status, contract, entry_date, entry_price, updated_at) "
        "VALUES (1, :status, :contract, :entry_date, :entry_price, :updated_at)",
        row,
    )
    conn.commit()
