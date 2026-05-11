"""SQLite database for trades, debates, and metrics."""

import os
import sqlite3
import json
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Optional, Any


def _resolve_db_path() -> Path:
    """Pick the SQLite file location with env-var override.

    Priority:
      1. ``TRADINGAGENTS_DB_PATH`` — explicit full path to the .db file.
      2. ``TRADINGAGENTS_HOME`` (or auto-resolved ``/data``) — base directory.
         When the base is exactly ``/data`` we drop the redundant ``data/``
         sub-folder and place the DB at ``/data/trades.db`` (cleaner cloud
         volume layout). For any other home, we still nest under ``data/``
         so the DB sits alongside reports/, memory/, etc.
      3. Fallback: ``data/trades.db`` next to this file (legacy local path).

    Cloud deployments should mount a persistent volume at ``/data`` so trades
    survive container restarts and capital persistence (which reads from the
    ``daily_metrics`` table) keeps working across days.
    """
    explicit = os.getenv("TRADINGAGENTS_DB_PATH")
    if explicit:
        return Path(explicit).expanduser()
    # Defer to default_config._resolve_home so the cloud-volume detection
    # logic (TRADINGAGENTS_HOME → /data → ~/.tradingagents) lives in one place.
    from tradingagents.default_config import _resolve_home
    home = Path(_resolve_home()).expanduser()
    # Drop the redundant `data/` subdir when the home IS the conventional
    # `/data` cloud volume — gives a cleaner layout there. For any other home
    # (including ~/.tradingagents) keep the `data/` subdir so the DB doesn't
    # collide with sibling files like memory/ and logs/.
    if str(home) == "/data":
        return home / "trades.db"
    return home / "data" / "trades.db"


DB_PATH = _resolve_db_path()


def get_conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_conn()
    # Enable WAL so multiple writer threads (parallel Phase 2) don't deadlock
    # on the database lock. Persistent across opens once set.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS trade_plans (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        date TEXT NOT NULL,
        ticker TEXT NOT NULL,
        rating TEXT,
        entry_zone_low REAL,
        entry_zone_high REAL,
        stop_loss REAL,
        target_1 REAL,
        target_2 REAL,
        confidence_score INTEGER,
        position_size_pct REAL,
        skip_rule TEXT,
        thesis TEXT,
        price_adjusted_pct REAL DEFAULT 0,
        is_dry_run INTEGER DEFAULT 0,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    );

    CREATE TABLE IF NOT EXISTS debates (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        date TEXT NOT NULL,
        ticker TEXT NOT NULL,
        round_num INTEGER,
        bull_argument TEXT,
        bear_argument TEXT,
        verdict TEXT,
        confidence INTEGER,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    );

    CREATE TABLE IF NOT EXISTS positions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        date TEXT NOT NULL,
        ticker TEXT NOT NULL,
        quantity INTEGER,
        entry_price REAL,
        exit_price REAL,
        stop_loss REAL,
        target_1 REAL,
        target_2 REAL,
        status TEXT,
        exit_reason TEXT,
        pnl REAL,
        pnl_pct REAL,
        opened_at TEXT,
        closed_at TEXT,
        is_dry_run INTEGER DEFAULT 0
    );

    CREATE TABLE IF NOT EXISTS daily_metrics (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        date TEXT NOT NULL UNIQUE,
        capital REAL,
        daily_pnl REAL,
        daily_return_pct REAL,
        total_trades INTEGER,
        win_rate REAL,
        max_drawdown_pct REAL,
        notes TEXT
    );

    CREATE TABLE IF NOT EXISTS agent_reports (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        date TEXT NOT NULL,
        ticker TEXT NOT NULL,
        agent_type TEXT NOT NULL,
        report TEXT,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    );

    -- DB-backed runtime configuration (replaces the old static DEFAULT_CONFIG
    -- dict for everything except the seed). Edited via /api/config/* endpoints
    -- and queried at the start of each pipeline run plus on every poll cycle.
    CREATE TABLE IF NOT EXISTS app_config (
        key         TEXT PRIMARY KEY,
        value       TEXT NOT NULL,           -- JSON-encoded so all types round-trip
        category    TEXT NOT NULL,
        is_secret   INTEGER NOT NULL DEFAULT 0,
        description TEXT,
        updated_at  TEXT DEFAULT CURRENT_TIMESTAMP
    );

    -- Audit trail of every config edit. Useful for debugging "why did the
    -- agents start using a different model overnight?" — every PATCH writes
    -- an entry here.
    CREATE TABLE IF NOT EXISTS config_changes (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        key         TEXT NOT NULL,
        old_value   TEXT,
        new_value   TEXT,
        changed_at  TEXT DEFAULT CURRENT_TIMESTAMP
    );

    -- Single-row state machine driving the cron dispatcher. The CHECK (id=1)
    -- guarantees we never accidentally insert a second row — the pipeline
    -- has exactly one state at any time. Read on every dispatcher tick
    -- (~every 60-3600s) and updated by the state-machine handlers.
    CREATE TABLE IF NOT EXISTS pipeline_state (
        id              INTEGER PRIMARY KEY CHECK (id = 1),
        state           TEXT    NOT NULL,
        state_since     TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP,
        trade_date      TEXT,
        next_run_at     TEXT,
        last_error      TEXT,
        payload         TEXT
    );

    -- Append-only audit trail of every state transition. Useful for
    -- debugging "why did we skip Tuesday's trades?" — every transition
    -- (including holiday detections and manual overrides via the API)
    -- writes a row here.
    CREATE TABLE IF NOT EXISTS pipeline_state_history (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        from_state      TEXT,
        to_state        TEXT NOT NULL,
        at              TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        note            TEXT
    );
    CREATE TABLE IF NOT EXISTS token_usage (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        date        TEXT NOT NULL,
        ticker      TEXT,
        stage       TEXT NOT NULL,
        model       TEXT,
        llm_calls   INTEGER NOT NULL DEFAULT 0,
        tool_calls  INTEGER NOT NULL DEFAULT 0,
        tokens_in   INTEGER NOT NULL DEFAULT 0,
        tokens_out  INTEGER NOT NULL DEFAULT 0,
        created_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    CREATE INDEX IF NOT EXISTS idx_token_usage_date ON token_usage(date);

    CREATE TABLE IF NOT EXISTS on_demand_analyses (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        ticker        TEXT NOT NULL,
        requested_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        completed_at  TEXT,
        status        TEXT NOT NULL DEFAULT 'pending',
        report_path   TEXT,
        error         TEXT,
        summary       TEXT
    );
    """)

    # Migrate trade_plans: add columns introduced after initial schema.
    # SQLite has no IF NOT EXISTS for ALTER TABLE, so check PRAGMA first.
    cols = {row[1] for row in conn.execute("PRAGMA table_info(trade_plans)").fetchall()}
    if "exclude_from_feedback" not in cols:
        conn.execute("ALTER TABLE trade_plans ADD COLUMN exclude_from_feedback INTEGER NOT NULL DEFAULT 0")
    if "price_adjusted_pct" not in cols:
        conn.execute("ALTER TABLE trade_plans ADD COLUMN price_adjusted_pct REAL DEFAULT 0")
    if "is_dry_run" not in cols:
        conn.execute("ALTER TABLE trade_plans ADD COLUMN is_dry_run INTEGER DEFAULT 0")
    pos_cols = {row[1] for row in conn.execute("PRAGMA table_info(positions)").fetchall()}
    if "is_dry_run" not in pos_cols:
        conn.execute("ALTER TABLE positions ADD COLUMN is_dry_run INTEGER DEFAULT 0")
    conn.commit()

    # Seed app_config from DEFAULT_CONFIG. Idempotent — INSERT OR IGNORE
    # leaves any existing user edits alone and only fills in missing keys
    # (e.g., when a new field is added in a future version).
    _seed_default_config_if_empty(conn)
    # Seed the single pipeline_state row in the same idempotent style.
    from tradingagents.dataflows.indian_market import IST
    conn.execute(
        "INSERT OR IGNORE INTO pipeline_state (id, state, state_since) "
        "VALUES (1, 'idle', ?)",
        (datetime.now(IST).isoformat(),),
    )
    conn.commit()
    conn.close()


def _seed_default_config_if_empty(conn: sqlite3.Connection) -> None:
    """Populate app_config rows from default_config.DEFAULT_CONFIG.

    Runs on every init_db() call. Behavior:
      - Fresh DB / empty table → all ~50 keys inserted.
      - Existing rows → no-op for those keys (INSERT OR IGNORE).
      - New key added in DEFAULT_CONFIG since last run → just the new key
        is inserted; existing user edits to other keys are preserved.

    Lives in this file (not config_service.py) to avoid a circular import:
    init_db() runs at module import time in some entry-points, before the
    web service module is loaded.
    """
    from tradingagents.default_config import DEFAULT_CONFIG, CONFIG_METADATA

    for key, value in DEFAULT_CONFIG.items():
        meta = CONFIG_METADATA.get(key, {})
        conn.execute(
            "INSERT OR IGNORE INTO app_config "
            "(key, value, category, is_secret, description) VALUES (?, ?, ?, ?, ?)",
            (
                key,
                json.dumps(value),
                meta.get("category", "uncategorized"),
                int(meta.get("is_secret", False)),
                meta.get("description", ""),
            ),
        )


def update_trade_plan_levels(plan: dict, price_adjusted_pct: float) -> None:
    """Update entry zone, SL, and targets after live-price adjustment at execution time."""
    conn = get_conn()
    try:
        conn.execute("""
            UPDATE trade_plans
            SET entry_zone_low=?, entry_zone_high=?, stop_loss=?, target_1=?, target_2=?,
                price_adjusted_pct=?
            WHERE ticker=? AND date=?
        """, (
            plan["entry_zone_low"], plan["entry_zone_high"],
            plan["stop_loss"], plan["target_1"], plan.get("target_2"),
            price_adjusted_pct,
            plan["ticker"], plan.get("date", datetime.now().strftime("%Y-%m-%d")),
        ))
        conn.commit()
    finally:
        conn.close()


def insert_trade_plan(plan: dict):
    conn = get_conn()
    try:
        conn.execute("""
            INSERT INTO trade_plans
            (date, ticker, rating, entry_zone_low, entry_zone_high, stop_loss,
             target_1, target_2, confidence_score, position_size_pct, skip_rule, thesis, is_dry_run)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            plan.get("date", datetime.now().strftime("%Y-%m-%d")),
            plan.get("ticker"),
            plan.get("rating"),
            plan.get("entry_zone_low"),
            plan.get("entry_zone_high"),
            plan.get("stop_loss"),
            plan.get("target_1"),
            plan.get("target_2"),
            plan.get("confidence_score"),
            plan.get("position_size_pct"),
            plan.get("skip_rule"),
            plan.get("thesis"),
            1 if plan.get("is_dry_run") else 0,
        ))
        conn.commit()
    finally:
        conn.close()


def insert_debate(debate: dict):
    conn = get_conn()
    try:
        conn.execute("""
            INSERT INTO debates
            (date, ticker, round_num, bull_argument, bear_argument, verdict, confidence)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (
            debate.get("date", datetime.now().strftime("%Y-%m-%d")),
            debate.get("ticker"),
            debate.get("round_num", 1),
            debate.get("bull_argument"),
            debate.get("bear_argument"),
            debate.get("verdict"),
            debate.get("confidence"),
        ))
        conn.commit()
    finally:
        conn.close()


def insert_position(position: dict):
    conn = get_conn()
    try:
        conn.execute("""
            INSERT INTO positions
            (date, ticker, quantity, entry_price, exit_price, stop_loss,
             target_1, target_2, status, exit_reason, pnl, pnl_pct, opened_at, closed_at, is_dry_run)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            position.get("date", datetime.now().strftime("%Y-%m-%d")),
            position.get("ticker"),
            position.get("quantity"),
            position.get("entry_price"),
            position.get("exit_price"),
            position.get("stop_loss"),
            position.get("target_1"),
            position.get("target_2"),
            position.get("status"),
            position.get("exit_reason"),
            position.get("pnl"),
            position.get("pnl_pct"),
            position.get("opened_at"),
            position.get("closed_at"),
            1 if position.get("is_dry_run") else 0,
        ))
        conn.commit()
    finally:
        conn.close()


def update_position_exit(ticker: str, date: str, exit_data: dict):
    """Update an open position row with exit data instead of inserting a new row."""
    conn = get_conn()
    try:
        conn.execute("""
            UPDATE positions SET
                exit_price = ?, exit_reason = ?, pnl = ?, pnl_pct = ?,
                status = 'closed', closed_at = ?
            WHERE ticker = ? AND date = ? AND status = 'open'
        """, (
            exit_data.get("exit_price"),
            exit_data.get("exit_reason"),
            exit_data.get("pnl"),
            exit_data.get("pnl_pct"),
            exit_data.get("closed_at"),
            ticker, date,
        ))
        conn.commit()
    finally:
        conn.close()


def insert_daily_metrics(metrics: dict):
    conn = get_conn()
    try:
        conn.execute("""
            INSERT OR REPLACE INTO daily_metrics
            (date, capital, daily_pnl, daily_return_pct, total_trades, win_rate, max_drawdown_pct, notes)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            metrics.get("date", datetime.now().strftime("%Y-%m-%d")),
            metrics.get("capital"),
            metrics.get("daily_pnl"),
            metrics.get("daily_return_pct"),
            metrics.get("total_trades"),
            metrics.get("win_rate"),
            metrics.get("max_drawdown_pct"),
            metrics.get("notes"),
        ))
        conn.commit()
    finally:
        conn.close()


def insert_agent_report(report: dict):
    conn = get_conn()
    try:
        conn.execute("""
            INSERT INTO agent_reports
            (date, ticker, agent_type, report)
            VALUES (?, ?, ?, ?)
        """, (
            report.get("date", datetime.now().strftime("%Y-%m-%d")),
            report.get("ticker"),
            report.get("agent_type"),
            report.get("report"),
        ))
        conn.commit()
    finally:
        conn.close()


def get_trade_plans(date: Optional[str] = None) -> List[dict]:
    conn = get_conn()
    try:
        if date:
            rows = conn.execute("SELECT * FROM trade_plans WHERE date = ? ORDER BY created_at DESC", (date,)).fetchall()
        else:
            rows = conn.execute("SELECT * FROM trade_plans ORDER BY created_at DESC LIMIT 50").fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_debates(date: Optional[str] = None, ticker: Optional[str] = None) -> List[dict]:
    conn = get_conn()
    try:
        query = "SELECT * FROM debates WHERE 1=1"
        params = []
        if date:
            query += " AND date = ?"
            params.append(date)
        if ticker:
            query += " AND ticker = ?"
            params.append(ticker)
        query += " ORDER BY created_at DESC LIMIT 100"
        rows = conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_positions(status: Optional[str] = None) -> List[dict]:
    conn = get_conn()
    try:
        if status:
            rows = conn.execute("SELECT * FROM positions WHERE status = ? ORDER BY opened_at DESC", (status,)).fetchall()
        else:
            rows = conn.execute("SELECT * FROM positions ORDER BY opened_at DESC LIMIT 100").fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_daily_metrics() -> List[dict]:
    conn = get_conn()
    try:
        rows = conn.execute("SELECT * FROM daily_metrics ORDER BY date DESC LIMIT 90").fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_latest_capital(default: float, before_date: Optional[str] = None) -> float:
    """Return the most recently recorded end-of-day capital, or `default`
    if no prior day has been recorded yet.

    When ``before_date`` is supplied (YYYY-MM-DD), only rows strictly older
    than that date are considered. This is what makes capital persist across
    days: each morning the pipeline asks "what did yesterday end at?" and uses
    that as today's starting capital. For historical dry-runs we pass
    ``before_date=date_being_simulated`` so today's own previous run (if any)
    doesn't double-count.
    """
    conn = get_conn()
    try:
        if before_date:
            row = conn.execute(
                "SELECT capital FROM daily_metrics WHERE date < ? ORDER BY date DESC LIMIT 1",
                (before_date,),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT capital FROM daily_metrics ORDER BY date DESC LIMIT 1"
            ).fetchone()
        if row and row["capital"] is not None:
            return float(row["capital"])
        return float(default)
    finally:
        conn.close()


def get_agent_reports(date: Optional[str] = None, ticker: Optional[str] = None) -> List[dict]:
    conn = get_conn()
    try:
        query = "SELECT * FROM agent_reports WHERE 1=1"
        params = []
        if date:
            query += " AND date = ?"
            params.append(date)
        if ticker:
            query += " AND ticker = ?"
            params.append(ticker)
        query += " ORDER BY created_at DESC LIMIT 200"
        rows = conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()
