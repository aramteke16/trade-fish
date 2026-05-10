"""Cron dispatcher — single APScheduler job that drives the 4-stage pipeline.

Architecture: one ``BackgroundScheduler`` runs alongside FastAPI in the same
Python process. It has exactly one job (``"dispatcher"``) that wakes on a
state-dependent interval (60-3600 seconds), reads the current
``pipeline_state`` row, dispatches to the matching handler, and reschedules
itself for the next wake.

Why one job, not six: APScheduler's ``reschedule_job`` makes dynamic
intervals trivial, and a single dispatcher serializes all stage transitions
through one code path — no race between two cron jobs both trying to
transition state.

State diagram (more detail in
[state_machine.py](state_machine.py)):

    idle ──[≥08:10 IST]──► precheck ──► waiting (or holiday/idle on no plans)
    waiting ──[≥09:30 IST]──► monitor ──[≥15:15 IST]──► analysis ──► idle

Per-day instances (PaperTrader, MarketMonitor) are cached in
``_daily_runtime`` keyed by trade date so all stages within a single
trading day see the same paper-trader balance, the same open positions,
etc. Cross-day state lives in SQLite (capital persistence reads the
latest ``daily_metrics`` row), so a process restart never breaks
continuity.
"""

from __future__ import annotations

import logging
import threading
import traceback
from datetime import datetime
from typing import Any, Optional

from apscheduler.schedulers.base import BaseScheduler

from tradingagents.dataflows.indian_market import IST
from tradingagents.pipeline import state_machine as sm
from tradingagents.pipeline.state_machine import (
    STATE_ANALYSIS,
    STATE_HOLIDAY,
    STATE_IDLE,
    STATE_MONITOR,
    STATE_PRECHECK,
    STATE_WAITING,
)
from tradingagents.web.config_service import load_config

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Process-wide scheduler reference + per-day runtime cache
# ---------------------------------------------------------------------------

# Set by ``register_dispatcher`` at FastAPI startup. The handlers reach back
# to this to call ``reschedule_job``. Module-level so handlers don't need to
# pass the scheduler through every function signature.
_SCHEDULER: Optional[BaseScheduler] = None

# Per-day singletons. Keyed by trade date (YYYY-MM-DD). Reset whenever we
# transition into ``precheck`` for a new day so memory doesn't accumulate.
_daily_runtime: dict[str, dict[str, Any]] = {}
_runtime_lock = threading.Lock()

DISPATCHER_JOB_ID = "dispatcher"


# ---------------------------------------------------------------------------
# Registration (called from FastAPI lifespan)
# ---------------------------------------------------------------------------


def register_dispatcher(scheduler: BaseScheduler) -> None:
    """Install the dispatcher job on the given scheduler. Idempotent — safe
    to call multiple times during testing without leaking duplicate jobs.
    """
    global _SCHEDULER
    _SCHEDULER = scheduler

    # Drop any pre-existing dispatcher (e.g. across a hot reload).
    try:
        scheduler.remove_job(DISPATCHER_JOB_ID)
    except Exception:
        pass

    # Initial interval is the idle default; the first tick will reschedule
    # based on the actual state.
    cfg = load_config()
    initial_interval = int(cfg.get("dispatcher_idle_interval_sec", 3600))
    scheduler.add_job(
        dispatch_pipeline,
        trigger="interval",
        seconds=initial_interval,
        id=DISPATCHER_JOB_ID,
        misfire_grace_time=300,
        coalesce=True,
        max_instances=1,
        next_run_time=datetime.now(IST),    # fire once immediately
    )
    logger.info("dispatcher registered (initial interval %ds)", initial_interval)


def _reschedule(seconds: int) -> None:
    """Update the dispatcher's interval on the running scheduler. No-op if
    the scheduler isn't registered yet (e.g. unit tests calling handlers
    directly)."""
    if _SCHEDULER is None:
        return
    try:
        _SCHEDULER.reschedule_job(
            DISPATCHER_JOB_ID, trigger="interval", seconds=int(seconds)
        )
    except Exception as e:
        logger.warning("dispatcher reschedule(%ds) failed: %s", seconds, e)


# ---------------------------------------------------------------------------
# Per-day runtime cache
# ---------------------------------------------------------------------------


def _get_runtime(trade_date: str) -> dict[str, Any]:
    """Return the per-day runtime dict (creates an empty one if needed).

    Keys we use:
      - paper_trader: PaperTrader instance for the day
      - monitor:      MarketMonitor instance bound to that trader
      - plans:        List[dict] of actionable plans from precheck
      - order_ids:    List[str] of orders placed during waiting→monitor
    """
    with _runtime_lock:
        # Drop any stale entries from previous days to bound memory.
        for date_key in list(_daily_runtime.keys()):
            if date_key != trade_date:
                _daily_runtime.pop(date_key, None)
        return _daily_runtime.setdefault(trade_date, {})


def _clear_runtime() -> None:
    with _runtime_lock:
        _daily_runtime.clear()


# ---------------------------------------------------------------------------
# Dispatcher entry point — the APScheduler job
# ---------------------------------------------------------------------------


def dispatch_pipeline() -> None:
    """Single tick of the dispatcher. Reads state, dispatches, reschedules.

    Wraps every handler in try/except so a handler exception:
      1. Records the traceback into pipeline_state.last_error,
      2. Forces the state to idle (so we don't get stuck mid-stage),
      3. Reschedules the dispatcher to the idle interval (1 hr default).
    """
    cfg = load_config()
    now = datetime.now(IST)
    state_row = sm.read_state()
    logger.info("dispatcher tick: state=%s, now=%s", state_row.state, now.strftime("%H:%M:%S"))

    # Hard global shortcut: market closed → don't bother running any handler.
    # Exception: if we're already in ``analysis``, let it run so EOD reflection
    # completes even on a Friday after market close.
    if (
        sm.is_market_closed(now.date())
        and state_row.state not in (STATE_ANALYSIS, STATE_HOLIDAY)
    ):
        sm.transition_to(STATE_HOLIDAY, note=f"market closed on {now.date().isoformat()}")
        _reschedule(int(cfg.get("dispatcher_idle_interval_sec", 3600)))
        return

    handler = STATE_HANDLERS.get(state_row.state)
    if handler is None:
        logger.error("unknown state %r; resetting to idle", state_row.state)
        sm.transition_to(STATE_IDLE, note=f"recovered from unknown state {state_row.state!r}")
        _reschedule(int(cfg.get("dispatcher_idle_interval_sec", 3600)))
        return

    try:
        next_state, next_interval = handler(now, state_row, cfg)
    except Exception:
        tb = traceback.format_exc()
        logger.exception("dispatcher handler %s threw", state_row.state)
        sm.transition_to(
            STATE_IDLE,
            last_error=tb,
            note=f"handler {state_row.state!r} crashed",
        )
        _reschedule(int(cfg.get("dispatcher_idle_interval_sec", 3600)))
        return

    sm.transition_to(next_state, trade_date=state_row.trade_date)
    _reschedule(next_interval)


# ---------------------------------------------------------------------------
# Per-state handlers
# ---------------------------------------------------------------------------
#
# Each handler signature: (now: datetime, state_row: StateRow, cfg: dict)
#                          -> (next_state: str, next_interval_sec: int)
#
# Handlers MUST be idempotent — they run on every dispatcher tick while
# their state is current, not just once.
# ---------------------------------------------------------------------------


def handle_idle(now: datetime, state_row: sm.StateRow, cfg: dict) -> tuple[str, int]:
    """Wake hourly. If we've crossed the precheck_time threshold today and
    the market is open, transition to precheck. Otherwise stay idle."""
    idle_sec = int(cfg.get("dispatcher_idle_interval_sec", 3600))
    if not sm.at_or_after(now, cfg.get("precheck_time", "08:10")):
        return STATE_IDLE, idle_sec
    # Market closed check is already handled in dispatch_pipeline; if we
    # got here, today is a trading day.
    return STATE_PRECHECK, int(cfg.get("dispatcher_waiting_interval_sec", 60))


def handle_precheck(now: datetime, state_row: sm.StateRow, cfg: dict) -> tuple[str, int]:
    """One-shot. Run screener + multi-agent analysis. On success, hand the
    actionable plans off to the runtime cache and transition to waiting.

    Failure modes:
      - No stocks pass the screener → idle until tomorrow.
      - All Skip after analysis → idle (no orders to place).
      - Exception during analysis → dispatcher's outer handler catches it
        and resets to idle with last_error populated.
    """
    trade_date = now.strftime("%Y-%m-%d")
    runtime = _get_runtime(trade_date)
    runtime.clear()  # fresh slate for the day

    # Defer the import — keeps dispatcher importable even when run_pipeline
    # itself can't load (e.g. missing optional deps in unit tests).
    from run_pipeline import run_analysis_phase, run_screener

    top_n = int(cfg.get("top_k_positions", 3))
    logger.info("[precheck] running screener (top_n=%d)", top_n)
    top_stocks = run_screener(top_n=top_n)
    if not top_stocks:
        logger.info("[precheck] no stocks passed screening; back to idle")
        return STATE_IDLE, int(cfg.get("dispatcher_idle_interval_sec", 3600))

    plans = run_analysis_phase(top_stocks, date=trade_date)
    runtime["plans"] = plans
    if not plans:
        logger.info("[precheck] no actionable plans; back to idle")
        return STATE_IDLE, int(cfg.get("dispatcher_idle_interval_sec", 3600))

    logger.info("[precheck] %d actionable plans ready; transitioning to waiting", len(plans))
    return STATE_WAITING, int(cfg.get("dispatcher_waiting_interval_sec", 60))


def handle_waiting(now: datetime, state_row: sm.StateRow, cfg: dict) -> tuple[str, int]:
    """Wakes every minute. At/after execution_time, place orders and
    transition to monitor. Before execution_time, stay in waiting."""
    waiting_sec = int(cfg.get("dispatcher_waiting_interval_sec", 60))
    monitor_sec = int(cfg.get("dispatcher_monitor_interval_sec", 600))
    exec_time = cfg.get("execution_time", "09:30")

    if not sm.at_or_after(now, exec_time):
        return STATE_WAITING, waiting_sec

    trade_date = now.strftime("%Y-%m-%d")
    runtime = _get_runtime(trade_date)
    plans = runtime.get("plans", [])
    if not plans:
        # Lost runtime cache (e.g. process restart between precheck and now).
        # Re-run the analysis to recover. This is the price of in-memory
        # plan storage; the alternative is persisting plans to DB, which we
        # already do via insert_trade_plan but in a flatter shape.
        logger.warning("[waiting] no cached plans; rerunning precheck")
        return handle_precheck(now, state_row, cfg)

    from run_pipeline import run_execution_phase
    from tradingagents.execution.paper_trader import PaperTrader
    from tradingagents.web.database import get_latest_capital

    starting_capital = get_latest_capital(
        default=cfg.get("initial_capital", 20000),
        before_date=trade_date,
    )
    paper_trader = PaperTrader(initial_capital=starting_capital)
    runtime["paper_trader"] = paper_trader
    logger.info("[waiting] starting capital ₹%.2f", starting_capital)

    order_ids = run_execution_phase(plans, paper_trader)
    runtime["order_ids"] = order_ids
    if not order_ids:
        logger.info("[waiting] no orders placed; skipping monitor")
        return STATE_ANALYSIS, monitor_sec   # straight to analysis to record metrics

    logger.info("[waiting] %d orders placed; transitioning to monitor", len(order_ids))
    return STATE_MONITOR, monitor_sec


def handle_monitor(now: datetime, state_row: sm.StateRow, cfg: dict) -> tuple[str, int]:
    """Wakes every 10 min. Runs one MarketMonitor.tick() — the trailing-stop
    ladder, news classifier, and price tick forwarding all happen there.

    When the execution window closes (15:15 IST), tick() returns True after
    running hard-exit-all internally. We then transition to analysis."""
    monitor_sec = int(cfg.get("dispatcher_monitor_interval_sec", 600))
    trade_date = now.strftime("%Y-%m-%d")
    runtime = _get_runtime(trade_date)

    paper_trader = runtime.get("paper_trader")
    if paper_trader is None:
        # Process restart mid-monitor — we lost the in-memory paper trader.
        # The DB has the trade plans saved but not the live PaperTrader's
        # in-flight orders; the safest recovery is to skip monitor and go
        # straight to analysis (where reporting will save whatever we have).
        logger.warning("[monitor] no cached paper_trader; jumping to analysis")
        return STATE_ANALYSIS, 60

    monitor = runtime.get("monitor")
    if monitor is None:
        from tradingagents.execution.risk_manager import RiskThresholds
        from tradingagents.pipeline.market_monitor import MarketMonitor

        risk = RiskThresholds(
            breakeven_trigger_pct=float(cfg.get("breakeven_trigger_pct", 0.5)),
            trail_trigger_pct=float(cfg.get("trail_trigger_pct", 1.0)),
            trail_lock_pct=float(cfg.get("trail_lock_pct", 0.3)),
        )
        # News monitor — opt-in via config, off-by-default fallback if the
        # FastClassifier can't initialize (missing API key).
        news_monitor = None
        if cfg.get("news_check_enabled", True):
            try:
                from tradingagents.llm_clients.fast_classifier import FastClassifier
                from tradingagents.pipeline.news_monitor import NewsMonitor
                news_monitor = NewsMonitor(
                    classifier=FastClassifier(),
                    lookback_min=int(cfg.get("news_check_lookback_min", 60)),
                )
            except Exception as e:
                logger.warning("[monitor] news monitor disabled: %s", e)

        monitor = MarketMonitor(
            paper_trader=paper_trader,
            poll_interval_sec=monitor_sec,
            risk_thresholds=risk,
            news_monitor=news_monitor,
        )
        runtime["monitor"] = monitor

    window_closed = monitor.tick(now)
    if window_closed:
        logger.info("[monitor] execution window closed; transitioning to analysis")
        return STATE_ANALYSIS, 60
    return STATE_MONITOR, monitor_sec


def handle_analysis(now: datetime, state_row: sm.StateRow, cfg: dict) -> tuple[str, int]:
    """One-shot. Run reporting phase (saves daily_metrics) and the EOD
    reflection sweep (writes per-trade lessons to the memory log).
    Transitions to idle so tomorrow's idle handler can fire precheck."""
    trade_date = now.strftime("%Y-%m-%d")
    runtime = _get_runtime(trade_date)
    paper_trader = runtime.get("paper_trader")

    if paper_trader is None:
        logger.warning("[analysis] no paper_trader; nothing to report")
        _clear_runtime()
        return STATE_IDLE, int(cfg.get("dispatcher_idle_interval_sec", 3600))

    from run_pipeline import run_eod_reflection, run_reporting_phase

    run_reporting_phase(paper_trader, date=trade_date)
    if cfg.get("eod_reflection_enabled", True):
        run_eod_reflection(paper_trader, date=trade_date)

    _clear_runtime()
    logger.info("[analysis] complete; back to idle")
    return STATE_IDLE, int(cfg.get("dispatcher_idle_interval_sec", 3600))


def handle_holiday(now: datetime, state_row: sm.StateRow, cfg: dict) -> tuple[str, int]:
    """Wake hourly. Once the date rolls forward to a non-holiday, transition
    back to idle so the next idle->precheck cycle can fire normally."""
    if sm.is_market_closed(now.date()):
        return STATE_HOLIDAY, int(cfg.get("dispatcher_idle_interval_sec", 3600))
    return STATE_IDLE, int(cfg.get("dispatcher_idle_interval_sec", 3600))


# Dispatch table — must include every state in ALL_STATES.
STATE_HANDLERS: dict[str, Any] = {
    STATE_IDLE: handle_idle,
    STATE_PRECHECK: handle_precheck,
    STATE_WAITING: handle_waiting,
    STATE_MONITOR: handle_monitor,
    STATE_ANALYSIS: handle_analysis,
    STATE_HOLIDAY: handle_holiday,
}
