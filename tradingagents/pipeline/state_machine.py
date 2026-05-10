"""Single-row state machine that drives the cron dispatcher.

The ``pipeline_state`` table holds exactly one row representing the current
stage of the daily trading cycle. The dispatcher
([dispatcher.py](dispatcher.py)) reads this state on every wake to decide
what to do next and how soon to wake again.

State transitions:

    idle ──[08:10 IST + market open]──► precheck ──► waiting
                                                │
              market closed ─────────────────► holiday ─► idle (next day)

    waiting ─[09:30 IST]─► monitor ─[15:15 IST]─► analysis ─[done]─► idle

This module exposes the read/write primitives — the dispatcher's per-state
handlers live in dispatcher.py.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import date as _date_t, datetime
from typing import Any, Optional

from tradingagents.dataflows.indian_market import IST, NSE_HOLIDAYS_2026
from tradingagents.web.database import get_conn

logger = logging.getLogger(__name__)


# State constants — the only legal values for pipeline_state.state.
# Keep these in sync with the dispatch_pipeline state-handler dispatch table.
STATE_IDLE = "idle"
STATE_PRECHECK = "precheck"
STATE_WAITING = "waiting"
STATE_MONITOR = "monitor"
STATE_ANALYSIS = "analysis"
STATE_HOLIDAY = "holiday"

ALL_STATES = (
    STATE_IDLE,
    STATE_PRECHECK,
    STATE_WAITING,
    STATE_MONITOR,
    STATE_ANALYSIS,
    STATE_HOLIDAY,
)


@dataclass
class StateRow:
    """In-memory mirror of the single pipeline_state row."""

    state: str
    state_since: str
    trade_date: Optional[str]
    next_run_at: Optional[str]
    last_error: Optional[str]
    payload: dict


# ---------------------------------------------------------------------------
# Read / write primitives
# ---------------------------------------------------------------------------


def read_state() -> StateRow:
    """Single SELECT of the current state row.

    Defensive: if the row is missing (database corruption or first-run race),
    seed an idle row and return it. This makes the dispatcher resilient to
    unexpected DB states without crashing the FastAPI process.
    """
    conn = get_conn()
    row = conn.execute(
        "SELECT state, state_since, trade_date, next_run_at, last_error, payload "
        "FROM pipeline_state WHERE id = 1"
    ).fetchone()
    if row is None:
        # Self-heal: re-insert the seed row.
        conn.execute(
            "INSERT OR REPLACE INTO pipeline_state (id, state, state_since) "
            "VALUES (1, ?, ?)",
            (STATE_IDLE, datetime.now(IST).isoformat()),
        )
        conn.commit()
        conn.close()
        logger.warning("pipeline_state row was missing; re-seeded as idle")
        return StateRow(
            state=STATE_IDLE,
            state_since=datetime.now(IST).isoformat(),
            trade_date=None,
            next_run_at=None,
            last_error=None,
            payload={},
        )

    payload_raw = row["payload"] or "{}"
    try:
        payload = json.loads(payload_raw)
    except (json.JSONDecodeError, TypeError):
        payload = {}
    conn.close()
    return StateRow(
        state=row["state"],
        state_since=row["state_since"],
        trade_date=row["trade_date"],
        next_run_at=row["next_run_at"],
        last_error=row["last_error"],
        payload=payload,
    )


def transition_to(
    new_state: str,
    *,
    trade_date: Optional[str] = None,
    payload: Optional[dict] = None,
    last_error: Optional[str] = None,
    next_run_at: Optional[datetime] = None,
    note: str = "",
) -> StateRow:
    """Atomically update the pipeline_state row AND append a row to history.

    Args:
      new_state: target state (must be one of ALL_STATES).
      trade_date: pinned to the current cycle's trade date so we can detect
        cross-day stale state on restart.
      payload: per-state dict (e.g. ``{"plan_count": 3, "actionable_tickers": [...]}``)
        serialized as JSON.
      last_error: traceback string from a failed handler; cleared on
        successful transitions.
      next_run_at: dispatcher's next scheduled wake (informational; the
        actual scheduling is done via reschedule_job in the dispatcher).
      note: human-readable explanation written to history.

    Returns the new StateRow.
    """
    if new_state not in ALL_STATES:
        raise ValueError(f"Unknown state {new_state!r}; valid: {ALL_STATES}")

    payload_json = json.dumps(payload or {})
    next_run_iso = next_run_at.isoformat() if next_run_at else None

    conn = get_conn()
    try:
        conn.execute("BEGIN")
        prev = conn.execute(
            "SELECT state FROM pipeline_state WHERE id = 1"
        ).fetchone()
        from_state = prev["state"] if prev else None

        now_ist = datetime.now(IST).isoformat()
        conn.execute(
            "UPDATE pipeline_state SET "
            "state = ?, state_since = ?, "
            "trade_date = ?, next_run_at = ?, last_error = ?, payload = ? "
            "WHERE id = 1",
            (new_state, now_ist, trade_date, next_run_iso, last_error, payload_json),
        )
        if from_state != new_state:
            conn.execute(
                "INSERT INTO pipeline_state_history (from_state, to_state, note) "
                "VALUES (?, ?, ?)",
                (from_state, new_state, note),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    if from_state != new_state:
        logger.info(
            "pipeline state: %s → %s%s",
            from_state, new_state, f" ({note})" if note else "",
        )
    return read_state()


def touch_state_since() -> None:
    """Update only state_since without writing to history. Used by the
    dispatcher on no-op ticks to keep the UI badge time fresh."""
    conn = get_conn()
    conn.execute(
        "UPDATE pipeline_state SET state_since = ? WHERE id = 1",
        (datetime.now(IST).isoformat(),),
    )
    conn.commit()
    conn.close()


def get_history(limit: int = 50) -> list[dict]:
    """Recent state transitions for the UI / debug view."""
    conn = get_conn()
    rows = conn.execute(
        "SELECT from_state, to_state, at, note FROM pipeline_state_history "
        "ORDER BY at DESC LIMIT ?",
        (limit,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Market-day predicate
# ---------------------------------------------------------------------------


def is_market_closed(for_date: _date_t) -> bool:
    """Return True if NSE is closed on ``for_date`` (weekend or holiday).

    Wraps the existing ``is_market_open()`` check so the dispatcher can
    short-circuit any state advance when the market is closed.
    """
    # Saturday=5, Sunday=6
    if for_date.weekday() >= 5:
        return True
    if for_date.strftime("%Y-%m-%d") in NSE_HOLIDAYS_2026:
        return True
    return False


# ---------------------------------------------------------------------------
# Helpers used by handlers
# ---------------------------------------------------------------------------


def parse_hhmm(hhmm: str) -> tuple[int, int]:
    """Parse a config-supplied HH:MM string to (hour, minute) ints.

    Defensive: returns (0, 0) on a malformed value rather than raising,
    because we don't want a typo'd config row to crash the whole dispatcher.
    """
    try:
        parts = hhmm.split(":")
        return int(parts[0]), int(parts[1])
    except (ValueError, IndexError, AttributeError):
        logger.warning("malformed time string %r; defaulting to 00:00", hhmm)
        return 0, 0


def at_or_after(now: datetime, hhmm: str) -> bool:
    """True iff ``now`` (must be tz-aware IST) is at or after the HH:MM wall
    time on the same day. Used by handlers to decide "fire stage X yet?"."""
    h, m = parse_hhmm(hhmm)
    return (now.hour, now.minute) >= (h, m)
