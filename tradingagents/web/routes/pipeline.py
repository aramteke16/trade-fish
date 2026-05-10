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
    note = body.note or "manual override via /api/pipeline/transition"
    new_row = sm.transition_to(body.to, note=note)
    return {
        "state": new_row.state,
        "state_since": new_row.state_since,
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

    try:
        next_state, next_interval = handler(now, state_row, cfg)
    except Exception as e:
        logger.exception("manual run-now %s threw", stage)
        raise HTTPException(status_code=500, detail=str(e))

    if next_state != state_row.state:
        sm.transition_to(next_state, note=f"manual run-now {stage!r}")

    # Reschedule the dispatcher so it picks up from the new state in the
    # right cadence — same as a normal tick.
    dispatcher._reschedule(int(next_interval))

    return {
        "ran_stage": stage,
        "next_state": next_state,
        "next_interval_sec": next_interval,
    }
