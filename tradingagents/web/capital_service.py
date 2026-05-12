"""DB-backed intraday capital state for the daily trading cycle.

Model (per the project's capital semantics):

  - seed_capital   = constant lifetime seed (DEFAULT_CONFIG["initial_capital"]).
  - start_capital  = today's starting capital (yesterday's EOD capital from
                     daily_metrics; equals seed for the first day).
  - free_cash      = cash available to place new orders. Drops as orders are
                     placed and rises as positions close.
  - invested       = capital locked in *filled* positions (entry_price * qty).
  - pending_reserved = capital reserved by *pending* (unfilled) orders
                     (entry_zone_high * qty).
  - daily_pnl      = realized P&L for the day so far.
  - current_value  = start_capital + daily_pnl  (live during the day).

Invariant (modulo rounding / charges):

    free_cash + invested + pending_reserved == start_capital + daily_pnl

Lifecycle:

  - ``init_day(date, start_capital)`` is called once at the start of the
    trading day (handle_waiting). Idempotent — if the row already exists for
    today, it is left alone.
  - ``snapshot(date, ...)`` is called after every event that mutates the
    capital buckets (order placed, fill, partial-exit, exit, expiration).
  - ``finalize_day(date)`` is called at the end of the day (handle_analysis).
    It writes the EOD ``capital`` column (= start_capital + daily_pnl) and
    sets ``is_finalized = 1`` so subsequent reads know the day is closed.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

from tradingagents.dataflows.indian_market import IST
from tradingagents.web.database import get_conn

logger = logging.getLogger(__name__)


def _today_ist() -> str:
    """IST-anchored YYYY-MM-DD. The pipeline's trading day is always IST,
    regardless of where the server runs (UTC containers would otherwise
    flip the date 5h30m too early)."""
    return datetime.now(IST).strftime("%Y-%m-%d")


def init_day(date: str, start_capital: float) -> None:
    """Seed today's row with start_capital. Idempotent.

    If a row already exists for ``date`` and ``start_capital`` is set, this
    is a no-op (preserves any in-flight snapshot). If the row exists but has
    no ``start_capital`` yet, fills it in.
    """
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT id, start_capital, is_finalized FROM daily_metrics WHERE date = ?",
            (date,),
        ).fetchone()
        if row is None:
            conn.execute(
                """
                INSERT INTO daily_metrics
                    (date, capital, start_capital, free_cash, invested,
                     pending_reserved, daily_pnl, is_finalized)
                VALUES (?, ?, ?, ?, 0, 0, 0, 0)
                """,
                (date, start_capital, start_capital, start_capital),
            )
            logger.info(
                "[capital] init_day %s start_capital=%.2f", date, start_capital
            )
        elif row["is_finalized"]:
            logger.debug(
                "[capital] init_day %s skipped (already finalized)", date
            )
        elif row["start_capital"] is None:
            conn.execute(
                """
                UPDATE daily_metrics
                SET start_capital = ?, free_cash = ?
                WHERE date = ?
                """,
                (start_capital, start_capital, date),
            )
            logger.info(
                "[capital] init_day %s back-filled start_capital=%.2f",
                date, start_capital,
            )
        conn.commit()
    finally:
        conn.close()


def snapshot(
    date: str,
    *,
    free_cash: float,
    invested: float,
    pending_reserved: float,
    daily_pnl: float,
) -> None:
    """Write the intraday capital buckets without touching start_capital
    or is_finalized. Safe to call on every mutation event."""
    conn = get_conn()
    try:
        conn.execute(
            """
            UPDATE daily_metrics
            SET free_cash = ?, invested = ?, pending_reserved = ?, daily_pnl = ?
            WHERE date = ? AND is_finalized = 0
            """,
            (free_cash, invested, pending_reserved, daily_pnl, date),
        )
        conn.commit()
    finally:
        conn.close()


def finalize_day(date: str) -> None:
    """Lock today's row: capital = start_capital + daily_pnl, is_finalized = 1.

    Idempotent. Skips if already finalized or if no row exists yet.
    """
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT start_capital, daily_pnl, is_finalized FROM daily_metrics WHERE date = ?",
            (date,),
        ).fetchone()
        if row is None:
            logger.warning("[capital] finalize_day %s: no row to finalize", date)
            return
        if row["is_finalized"]:
            return
        start = row["start_capital"]
        pnl = row["daily_pnl"] or 0.0
        if start is None:
            logger.warning(
                "[capital] finalize_day %s: missing start_capital, skipping",
                date,
            )
            return
        new_capital = float(start) + float(pnl)
        conn.execute(
            """
            UPDATE daily_metrics
            SET capital = ?, is_finalized = 1
            WHERE date = ?
            """,
            (new_capital, date),
        )
        conn.commit()
        logger.info(
            "[capital] finalize_day %s: capital %.2f (= start %.2f + pnl %.2f)",
            date, new_capital, float(start), float(pnl),
        )
    finally:
        conn.close()


def get_today(date: Optional[str] = None) -> Optional[dict]:
    """Return today's row including the intraday capital buckets."""
    date = date or _today_ist()
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT * FROM daily_metrics WHERE date = ?", (date,)
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def log_snapshot(
    date: str,
    *,
    start_capital: float,
    current_value: float,
    free_cash: float,
    invested: float,
    pending_reserved: float,
    realized_pnl: float,
    unrealized_pnl: float = 0.0,
    open_positions_count: int = 0,
    trigger: str = "tick",
) -> None:
    """Append one row to ``capital_log``. Cheap and append-only — drives the
    UI's intraday capital history table."""
    conn = get_conn()
    try:
        conn.execute(
            """
            INSERT INTO capital_log
                (date, current_value, start_capital, free_cash, invested,
                 pending_reserved, realized_pnl, unrealized_pnl,
                 open_positions_count, trigger)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                date, current_value, start_capital, free_cash, invested,
                pending_reserved, realized_pnl, unrealized_pnl,
                open_positions_count, trigger,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def get_log(date: Optional[str] = None, limit: int = 200) -> list[dict]:
    """Return the most recent ``limit`` capital_log rows for ``date``,
    newest first."""
    date = date or _today_ist()
    conn = get_conn()
    try:
        rows = conn.execute(
            """
            SELECT * FROM capital_log
            WHERE date = ?
            ORDER BY at DESC, id DESC
            LIMIT ?
            """,
            (date, limit),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()
