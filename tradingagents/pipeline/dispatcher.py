"""Cron dispatcher — fixed 60s APScheduler job driving the pipeline.

Architecture: one ``BackgroundScheduler`` fires every 60 seconds. Each tick
reads the current ``pipeline_state`` row from DB + the IST clock, dispatches
to the matching state handler, and transitions if needed.

Long-running handlers (precheck, waiting, analysis) are offloaded to a
single background thread so the 60s tick stays non-blocking. While a
background task runs, each tick updates ``state_since`` to keep the UI
badge timestamp fresh and logs elapsed time for observability.

State diagram:

    idle ──[now >= 08:10 AND not already ran today]──► precheck ──[done]──► waiting
    waiting ──[now >= 09:30]──► monitor
    monitor ──[now >= 15:15]──► analysis ──[done]──► idle
    holiday ──[next trading day]──► idle

Per-day instances (PaperTrader, MarketMonitor) are cached in
``_daily_runtime`` keyed by trade date. Cross-day state lives in SQLite.
"""

from __future__ import annotations

import logging
import threading
import time as _time
import traceback
from concurrent.futures import Future, ThreadPoolExecutor
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

_SCHEDULER: Optional[BaseScheduler] = None
_daily_runtime: dict[str, dict[str, Any]] = {}
_runtime_lock = threading.Lock()
DISPATCHER_JOB_ID = "dispatcher"

TICK_INTERVAL_SEC = 60

# Background executor for long-running handlers
_bg_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="pipeline-bg")
_background_future: Optional[Future] = None
_background_state: Optional[str] = None
_background_started_at: Optional[float] = None
_LONG_RUNNING_STATES = {STATE_PRECHECK, STATE_WAITING, STATE_ANALYSIS}

# Wall-clock time of last monitor.tick() call — used for throttling instead of
# state_since, which is reset every 60s tick and would prevent the poll from firing.
_last_monitor_tick_at: Optional[float] = None


def register_dispatcher(scheduler: BaseScheduler) -> None:
    """Install the fixed 60s dispatcher job. Idempotent."""
    global _SCHEDULER
    _SCHEDULER = scheduler
    try:
        scheduler.remove_job(DISPATCHER_JOB_ID)
    except Exception:
        pass
    scheduler.add_job(
        dispatch_pipeline,
        trigger="interval",
        seconds=TICK_INTERVAL_SEC,
        id=DISPATCHER_JOB_ID,
        misfire_grace_time=300,
        coalesce=True,
        max_instances=1,
        next_run_time=datetime.now(IST),
    )
    logger.info("dispatcher registered (fixed %ds tick)", TICK_INTERVAL_SEC)


# ---------------------------------------------------------------------------
# Per-day runtime cache
# ---------------------------------------------------------------------------

def _get_runtime(trade_date: str) -> dict[str, Any]:
    with _runtime_lock:
        for date_key in list(_daily_runtime.keys()):
            if date_key != trade_date:
                _daily_runtime.pop(date_key, None)
        return _daily_runtime.setdefault(trade_date, {})


def _clear_runtime() -> None:
    global _last_monitor_tick_at
    with _runtime_lock:
        _daily_runtime.clear()
    _last_monitor_tick_at = None


def get_active_paper_trader():
    """Return the current day's PaperTrader if one exists, else None.

    Used by the REST endpoint for manual position exits.
    """
    with _runtime_lock:
        for runtime in _daily_runtime.values():
            pt = runtime.get("paper_trader")
            if pt is not None:
                return pt
    return None


# ---------------------------------------------------------------------------
# Background task helpers
# ---------------------------------------------------------------------------

def _clear_background() -> None:
    global _background_future, _background_state, _background_started_at
    _background_future = None
    _background_state = None
    _background_started_at = None


def _cancel_background() -> None:
    """Cancel any in-flight background handler. Called by force-rerun API."""
    global _background_future, _last_monitor_tick_at
    if _background_future is not None and not _background_future.done():
        _background_future.cancel()
    _clear_background()
    _last_monitor_tick_at = None


# ---------------------------------------------------------------------------
# Dispatcher entry point
# ---------------------------------------------------------------------------

def dispatch_pipeline() -> None:
    """Single tick. Reads state from DB, dispatches handler, transitions.

    Long-running states (precheck, waiting, analysis) are offloaded to a
    background thread. Short states (idle, monitor, holiday) run inline.
    """
    global _background_future, _background_state, _background_started_at

    cfg = load_config()
    now = datetime.now(IST)
    state_row = sm.read_state()
    logger.info("dispatcher tick: state=%s, now=%s", state_row.state, now.strftime("%H:%M:%S"))

    # Market-closed check
    if (
        sm.is_market_closed(now.date())
        and state_row.state not in (STATE_ANALYSIS, STATE_HOLIDAY)
    ):
        sm.transition_to(STATE_HOLIDAY, note=f"market closed on {now.date().isoformat()}")
        _cancel_background()
        return

    handler = STATE_HANDLERS.get(state_row.state)
    if handler is None:
        logger.error("unknown state %r; resetting to idle", state_row.state)
        sm.transition_to(STATE_IDLE, note=f"recovered from unknown state {state_row.state!r}")
        _cancel_background()
        return

    # --- Background task handling for long-running states ---
    if state_row.state in _LONG_RUNNING_STATES:
        # Stale bg task from a different state (e.g. manual override) — cancel it
        if _background_future is not None and _background_state != state_row.state:
            logger.warning(
                "[bg] stale task for %s while state is %s; cancelling",
                _background_state, state_row.state,
            )
            _cancel_background()

        if _background_future is not None:
            if not _background_future.done():
                elapsed = _time.time() - (_background_started_at or 0)
                # Log only every 5 minutes to avoid spam
                if int(elapsed) % 300 < TICK_INTERVAL_SEC:
                    logger.info(
                        "[bg] %s still running (%.0fm elapsed)",
                        state_row.state, elapsed / 60,
                    )
                sm.touch_state_since()
                return

            # Task completed — harvest result
            try:
                next_state = _background_future.result()
            except Exception:
                tb = traceback.format_exc()
                logger.exception("[bg] handler %s crashed", state_row.state)
                sm.transition_to(
                    STATE_IDLE, last_error=tb,
                    note=f"handler {state_row.state!r} crashed (bg)",
                )
                _clear_background()
                return

            elapsed = _time.time() - (_background_started_at or 0)
            logger.info(
                "[bg] %s completed in %.1fs → next_state=%s",
                state_row.state, elapsed, next_state,
            )
            _clear_background()

            if next_state is not None and next_state != state_row.state:
                sm.transition_to(next_state, trade_date=state_row.trade_date)
            else:
                sm.touch_state_since()
            return

        # No background task running — spawn one
        _background_state = state_row.state
        _background_started_at = _time.time()
        _background_future = _bg_executor.submit(handler, now, state_row, cfg)
        logger.info("[bg] spawned background task for %s", state_row.state)
        sm.touch_state_since()
        return

    # --- Inline execution for short handlers (idle, monitor, holiday) ---
    try:
        next_state = handler(now, state_row, cfg)
    except Exception:
        tb = traceback.format_exc()
        logger.exception("dispatcher handler %s threw", state_row.state)
        sm.transition_to(STATE_IDLE, last_error=tb, note=f"handler {state_row.state!r} crashed")
        return

    if next_state is not None and next_state != state_row.state:
        sm.transition_to(next_state, trade_date=state_row.trade_date)
    else:
        sm.touch_state_since()


# ---------------------------------------------------------------------------
# Handlers — each returns next_state (str) or None to stay without updating
# ---------------------------------------------------------------------------

def handle_idle(now: datetime, state_row: sm.StateRow, cfg: dict) -> Optional[str]:
    if not sm.at_or_after(now, cfg.get("precheck_time", "08:10")):
        return None
    today = now.strftime("%Y-%m-%d")
    if sm.has_completed_today(today, "precheck"):
        return None
    return STATE_PRECHECK


def handle_precheck(now: datetime, state_row: sm.StateRow, cfg: dict) -> Optional[str]:
    """One-shot: screener + multi-agent analysis."""
    trade_date = now.strftime("%Y-%m-%d")
    runtime = _get_runtime(trade_date)
    runtime.clear()

    from run_pipeline import run_analysis_phase, run_screener

    dry_run = bool(cfg.get("dry_run_e2e", False))
    if dry_run:
        ticker = cfg.get("dry_run_ticker", "RELIANCE.NS")
        top_stocks = [{"ticker": ticker, "composite_score": 99, "price": 0, "avg_volume_inr_crores": 0, "atr_pct": 0}]
        logger.info("[precheck] DRY RUN: skipping screener, analyzing ticker=%s", ticker)
    else:
        top_n = int(cfg.get("top_k_positions", 3))
        logger.info("[precheck] running screener (top_n=%d)", top_n)
        top_stocks = run_screener(top_n=top_n)
        if not top_stocks:
            logger.info("[precheck] no stocks passed screening; back to idle")
            return STATE_IDLE

    plans = run_analysis_phase(top_stocks, date=trade_date)
    if dry_run:
        for p in plans:
            p["is_dry_run"] = True
    runtime["plans"] = plans
    if not plans:
        logger.info("[precheck] no actionable plans; back to idle")
        return STATE_IDLE

    logger.info("[precheck] %d actionable plans ready%s", len(plans), " [DRY RUN]" if dry_run else "")
    return STATE_WAITING


def handle_waiting(now: datetime, state_row: sm.StateRow, cfg: dict) -> Optional[str]:
    if not sm.at_or_after(now, cfg.get("execution_time", "09:30")):
        return None

    trade_date = now.strftime("%Y-%m-%d")
    runtime = _get_runtime(trade_date)
    plans = runtime.get("plans", [])
    if not plans:
        # In-memory cache lost (e.g. container restart) — reload from DB before
        # falling back to a full precheck rerun.
        from tradingagents.web.database import get_trade_plans
        db_plans = [p for p in get_trade_plans(trade_date) if p.get("rating") == "Buy"]
        if db_plans:
            logger.info("[waiting] reloaded %d plan(s) from DB after cache miss", len(db_plans))
            runtime["plans"] = db_plans
            plans = db_plans
        else:
            logger.warning("[waiting] no cached plans and no DB plans; rerunning precheck")
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
    logger.info("[waiting] starting capital %.2f", starting_capital)

    order_ids = run_execution_phase(plans, paper_trader)
    runtime["order_ids"] = order_ids
    if not order_ids:
        logger.info("[waiting] no orders placed; skipping monitor")
        return STATE_ANALYSIS

    logger.info("[waiting] %d orders placed", len(order_ids))
    return STATE_MONITOR


def handle_monitor(now: datetime, state_row: sm.StateRow, cfg: dict) -> Optional[str]:
    """Runs monitor.tick() throttled by dispatcher_monitor_interval_sec.
    Uses state_since to check elapsed time since last tick."""
    monitor_interval = 60 if cfg.get("dry_run_e2e") else int(cfg.get("dispatcher_monitor_interval_sec", 600))
    trade_date = now.strftime("%Y-%m-%d")
    runtime = _get_runtime(trade_date)

    paper_trader = runtime.get("paper_trader")
    if paper_trader is None:
        # In-memory cache lost (container restart while in monitor state).
        # Rebuild PaperTrader from today's DB plans so monitoring can continue.
        from tradingagents.web.database import get_trade_plans, get_latest_capital
        from tradingagents.execution.paper_trader import PaperTrader
        from run_pipeline import run_execution_phase
        db_plans = [p for p in get_trade_plans(trade_date) if p.get("rating") == "Buy"]
        if not db_plans:
            logger.warning("[monitor] no cached paper_trader and no DB plans; jumping to analysis")
            return STATE_ANALYSIS
        starting_capital = get_latest_capital(
            default=cfg.get("initial_capital", 20000),
            before_date=trade_date,
        )
        paper_trader = PaperTrader(initial_capital=starting_capital)
        run_execution_phase(db_plans, paper_trader)
        runtime["paper_trader"] = paper_trader
        logger.info("[monitor] rebuilt paper_trader from DB (%d plans)", len(db_plans))

    # Hard exit check
    if sm.at_or_after(now, cfg.get("hard_exit_time", "15:15")):
        monitor = runtime.get("monitor")
        if monitor:
            monitor.tick(now)
        logger.info("[monitor] hard exit time reached; transitioning to analysis")
        return STATE_ANALYSIS

    # Throttle using a wall-clock timestamp — NOT state_since.
    # state_since is touched on every 60s tick to keep the UI badge fresh,
    # which would reset elapsed to ~60s every tick and prevent the poll firing.
    global _last_monitor_tick_at
    now_ts = _time.time()
    elapsed = (now_ts - _last_monitor_tick_at) if _last_monitor_tick_at is not None else monitor_interval
    if elapsed < monitor_interval:
        logger.debug("[monitor] throttled: %.0fs elapsed < %ds interval", elapsed, monitor_interval)
        return None

    # Run the actual monitor tick
    monitor = runtime.get("monitor")
    if monitor is None:
        from tradingagents.execution.risk_manager import RiskThresholds
        from tradingagents.pipeline.market_monitor import MarketMonitor

        risk = RiskThresholds(
            breakeven_trigger_pct=float(cfg.get("breakeven_trigger_pct", 0.5)),
            trail_trigger_pct=float(cfg.get("trail_trigger_pct", 1.0)),
            trail_lock_pct=float(cfg.get("trail_lock_pct", 0.3)),
        )
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
            poll_interval_sec=monitor_interval,
            risk_thresholds=risk,
            news_monitor=news_monitor,
        )
        runtime["monitor"] = monitor

    window_closed = monitor.tick(now)
    _last_monitor_tick_at = _time.time()
    if window_closed:
        logger.info("[monitor] execution window closed; transitioning to analysis")
        return STATE_ANALYSIS

    return None


def handle_analysis(now: datetime, state_row: sm.StateRow, cfg: dict) -> Optional[str]:
    """One-shot: reporting + EOD reflection."""
    trade_date = now.strftime("%Y-%m-%d")
    runtime = _get_runtime(trade_date)
    paper_trader = runtime.get("paper_trader")

    if paper_trader is None:
        logger.warning("[analysis] no paper_trader; nothing to report")
        _clear_runtime()
        return STATE_IDLE

    from run_pipeline import run_eod_reflection, run_reporting_phase

    run_reporting_phase(paper_trader, date=trade_date)
    if cfg.get("eod_reflection_enabled", True):
        run_eod_reflection(paper_trader, date=trade_date)

    _clear_runtime()
    logger.info("[analysis] complete; back to idle")
    return STATE_IDLE


def handle_holiday(now: datetime, state_row: sm.StateRow, cfg: dict) -> Optional[str]:
    if sm.is_market_closed(now.date()):
        return None
    return STATE_IDLE


STATE_HANDLERS: dict[str, Any] = {
    STATE_IDLE: handle_idle,
    STATE_PRECHECK: handle_precheck,
    STATE_WAITING: handle_waiting,
    STATE_MONITOR: handle_monitor,
    STATE_ANALYSIS: handle_analysis,
    STATE_HOLIDAY: handle_holiday,
}
