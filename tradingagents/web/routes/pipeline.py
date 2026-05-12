"""REST API for the cron-dispatcher state machine.

Endpoints under ``/api/pipeline``:

  GET   /pipeline/state                  current state row + recent history
  POST  /pipeline/transition             force the state machine into a target
                                         state (operations override; e.g. "reset
                                         to idle if a stage got stuck")
  POST  /pipeline/run-now/{stage}        synchronously fire a stage handler now
                                         (testing convenience; respects the
                                         state machine guards)

These exist so the UI / curl can:
  - Watch the dispatcher live ("are we in monitor right now?")
  - Recover from a stuck state without redeploying
  - Trigger ad-hoc test runs of any stage
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from tradingagents.dataflows.indian_market import IST
from tradingagents.pipeline import dispatcher, state_machine as sm
from tradingagents.web.config_service import load_config
from tradingagents.web.database import get_conn

logger = logging.getLogger(__name__)

router = APIRouter()


class TransitionRequest(BaseModel):
    """Body for POST /pipeline/transition."""

    to: str
    note: Optional[str] = None


@router.get("/pipeline/state")
def get_pipeline_state():
    """Return the current state row + the last 20 transitions for the UI."""
    state_row = sm.read_state()
    return {
        "state": state_row.state,
        "state_since": state_row.state_since,
        "last_heartbeat_at": state_row.last_heartbeat_at,
        "trade_date": state_row.trade_date,
        "next_run_at": state_row.next_run_at,
        "last_error": state_row.last_error,
        "payload": state_row.payload,
        "history": sm.get_history(limit=20),
    }


@router.post("/pipeline/transition")
def post_transition(body: TransitionRequest):
    """Force the state machine into ``body.to``. The next dispatcher tick
    runs that state's handler. Use this to recover from a stuck state
    (e.g. an exception left ``state='precheck'`` but nothing is making
    progress); ``POST {"to": "idle"}`` resets cleanly."""
    if body.to not in sm.ALL_STATES:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown state {body.to!r}; valid: {list(sm.ALL_STATES)}",
        )
    if dispatcher.has_active_background():
        raise HTTPException(
            status_code=409,
            detail="A pipeline background task is still running; wait for it to finish before forcing a transition.",
        )
    note = body.note or "manual override via /api/pipeline/transition"
    trade_date = datetime.now(IST).strftime("%Y-%m-%d") if body.to != sm.STATE_IDLE else sm.read_state().trade_date
    new_row = sm.transition_to(body.to, trade_date=trade_date, note=note)
    return {
        "state": new_row.state,
        "state_since": new_row.state_since,
        "last_heartbeat_at": new_row.last_heartbeat_at,
        "note": note,
    }


@router.post("/pipeline/run-now/{stage}")
def post_run_now(stage: str):
    """Synchronously invoke a stage handler with the current clock.

    The handler runs to completion before returning, so this can take a
    while (precheck does the full multi-agent debate, ~5 min). Used for
    testing; production trigger is the dispatcher cron.
    """
    if stage not in sm.ALL_STATES:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown stage {stage!r}; valid: {list(sm.ALL_STATES)}",
        )

    handler = dispatcher.STATE_HANDLERS.get(stage)
    if handler is None:
        raise HTTPException(status_code=400, detail=f"No handler for {stage!r}")

    cfg = load_config()
    now = datetime.now(IST)
    state_row = sm.read_state()
    if dispatcher.has_active_background():
        raise HTTPException(
            status_code=409,
            detail="A pipeline background task is already running; run-now is disabled until it finishes.",
        )

    try:
        next_state = handler(now, state_row, cfg)
    except Exception as e:
        logger.exception("manual run-now %s threw", stage)
        raise HTTPException(status_code=500, detail=str(e))

    if next_state is not None and next_state != state_row.state:
        trade_date = state_row.trade_date or now.strftime("%Y-%m-%d")
        sm.transition_to(next_state, trade_date=trade_date, note=f"manual run-now {stage!r}")

    return {
        "ran_stage": stage,
        "next_state": next_state or state_row.state,
    }


@router.post("/pipeline/force-rerun")
def force_rerun():
    """Clear today's analysis data and restart precheck from scratch.

    Deletes trade_plans, agent_reports, debates, pipeline_state_history,
    and markdown report files for today. Cancels any in-flight background
    handler, then transitions to precheck.
    """
    import shutil
    from pathlib import Path

    today = datetime.now(IST).strftime("%Y-%m-%d")
    if not dispatcher._cancel_background():
        raise HTTPException(
            status_code=409,
            detail="A pipeline background task is still running; wait for it to finish before force rerun.",
        )

    conn = get_conn()
    try:
        conn.execute("DELETE FROM trade_plans WHERE date = ?", (today,))
        conn.execute("DELETE FROM agent_reports WHERE date = ?", (today,))
        conn.execute("DELETE FROM debates WHERE date = ?", (today,))
        conn.execute("DELETE FROM pipeline_state_history WHERE date(at) = ?", (today,))
        conn.commit()
    finally:
        conn.close()

    # Remove markdown report files for today
    try:
        reports_dir = load_config().get("reports_dir", "")
        if reports_dir:
            day_dir = Path(reports_dir) / today
            if day_dir.is_dir():
                shutil.rmtree(day_dir)
                logger.info("force-rerun: removed reports dir %s", day_dir)
    except Exception as e:
        logger.warning("force-rerun: could not remove reports: %s", e)

    new_row = sm.transition_to(sm.STATE_PRECHECK, trade_date=today, note="force rerun via UI")
    logger.info("force-rerun: cleared data for %s, transitioning to precheck", today)

    return {
        "status": "ok",
        "cleared_date": today,
        "state": new_row.state,
    }
