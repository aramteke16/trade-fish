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
from tradingagents.web import telegram_notifier as tg

logger = logging.getLogger(__name__)


def _tg(title: str, body: str = "", **fields) -> None:
    """Best-effort Telegram notification. Guaranteed non-throwing — a busted
    notifier must never crash the dispatcher."""
    try:
        tg.notify(title, body, **fields)
    except Exception as e:  # noqa: BLE001 - we really want a catch-all here
        logger.debug("[telegram] notify failed silently: %s", e)


def _maybe_send_morning_brief(now: datetime, cfg: dict) -> None:
    """Fire the daily morning brief exactly once per day.

    Triggered from every dispatcher tick: if today >= configured time
    AND we haven't sent today's brief yet (per the
    ``telegram_morning_message_last_date`` flag in app_config), send it
    and persist today's date so a reboot can't double-send.
    """
    if not cfg.get("telegram_notifications_enabled"):
        return
    if not cfg.get("telegram_morning_message_enabled", True):
        return
    if not sm.at_or_after(now, cfg.get("telegram_morning_message_time", "08:00")):
        return
    today = now.strftime("%Y-%m-%d")
    if str(cfg.get("telegram_morning_message_last_date") or "") == today:
        return
    try:
        sent = tg.notify_morning_brief(today)
        if sent:
            from tradingagents.web.config_service import set_config
            set_config("telegram_morning_message_last_date", today)
    except Exception as e:  # noqa: BLE001
        logger.debug("[telegram] morning brief send failed silently: %s", e)

_SCHEDULER: Optional[BaseScheduler] = None
_daily_runtime: dict[str, dict[str, Any]] = {}
_runtime_lock = threading.Lock()
DISPATCHER_JOB_ID = "dispatcher"

TICK_INTERVAL_SEC = 60

# Background executor for long-running handlers
_bg_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="pipeline-bg")
_bg_lock = threading.Lock()
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


def _cancel_background() -> bool:
    """Cancel an in-flight background handler if it has not started.

    Python cannot safely stop a thread that is already running. Returning
    False lets API callers reject destructive actions instead of clearing DB
    rows while the old handler may still write more data.
    """
    global _background_future, _last_monitor_tick_at
    with _bg_lock:
        if _background_future is not None and not _background_future.done():
            if not _background_future.cancel():
                return False
        _clear_background()
    _last_monitor_tick_at = None
    return True


def has_active_background() -> bool:
    """True when a long-running handler is currently executing."""
    with _bg_lock:
        return _background_future is not None and not _background_future.done()


def _trade_date_for_transition(now: datetime, state_row: sm.StateRow, next_state: str) -> Optional[str]:
    """Pin daily-cycle states to an IST trade date."""
    if next_state == STATE_HOLIDAY:
        return now.strftime("%Y-%m-%d")
    if next_state in (STATE_PRECHECK, STATE_WAITING, STATE_MONITOR, STATE_ANALYSIS):
        return state_row.trade_date or now.strftime("%Y-%m-%d")
    if next_state == STATE_IDLE:
        return state_row.trade_date
    return state_row.trade_date


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
    dry_run = bool(cfg.get("dry_run_e2e", False))
    logger.info(
        "dispatcher tick: state=%s, now=%s%s",
        state_row.state, now.strftime("%H:%M:%S"),
        " [DRY RUN]" if dry_run else "",
    )

    # Daily morning brief — fires from the first tick at or after the
    # configured time each day. Persisted via app_config so a reboot
    # later the same day can't double-send.
    _maybe_send_morning_brief(now, cfg)

    # Market-closed check. In dry-run mode we deliberately skip this so the
    # E2E flow can be exercised on weekends/holidays/off-hours.
    if (
        not dry_run
        and sm.is_market_closed(now.date())
        and state_row.state not in (STATE_ANALYSIS, STATE_HOLIDAY)
    ):
        if not _cancel_background():
            logger.warning("market-closed transition delayed; background task is still running")
            sm.touch_heartbeat()
            return
        sm.transition_to(
            STATE_HOLIDAY,
            trade_date=now.strftime("%Y-%m-%d"),
            note=f"market closed on {now.date().isoformat()}",
        )
        return

    handler = STATE_HANDLERS.get(state_row.state)
    if handler is None:
        logger.error("unknown state %r; resetting to idle", state_row.state)
        if not _cancel_background():
            logger.warning("unknown-state recovery delayed; background task is still running")
            sm.touch_heartbeat()
            return
        sm.transition_to(
            STATE_IDLE,
            trade_date=state_row.trade_date,
            note=f"recovered from unknown state {state_row.state!r}",
        )
        return

    # --- Background task handling for long-running states ---
    if state_row.state in _LONG_RUNNING_STATES:
        with _bg_lock:
            # Stale bg task from a different state (e.g. manual override) — cancel it
            if _background_future is not None and _background_state != state_row.state:
                logger.warning(
                    "[bg] stale task for %s while state is %s; cancelling",
                    _background_state, state_row.state,
                )
                if not _background_future.done():
                    if not _background_future.cancel():
                        sm.touch_heartbeat()
                        return
                _clear_background()

            if _background_future is not None:
                if not _background_future.done():
                    elapsed = _time.time() - (_background_started_at or 0)
                    if int(elapsed) % 300 < TICK_INTERVAL_SEC:
                        logger.info(
                            "[bg] %s still running (%.0fm elapsed)",
                            state_row.state, elapsed / 60,
                        )
                    sm.touch_heartbeat()
                    return

                # Task completed — harvest result
                try:
                    next_state = _background_future.result()
                except Exception:
                    tb = traceback.format_exc()
                    logger.exception("[bg] handler %s crashed", state_row.state)
                    _tg(
                        f"Pipeline crash · {state_row.state}",
                        body=tb.splitlines()[-1][:300] if tb else "no traceback",
                        state=state_row.state,
                        trade_date=state_row.trade_date,
                    )
                    sm.transition_to(
                        STATE_IDLE,
                        trade_date=state_row.trade_date,
                        last_error=tb,
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
                    sm.transition_to(
                        next_state,
                        trade_date=_trade_date_for_transition(now, state_row, next_state),
                    )
                else:
                    sm.touch_heartbeat()
                return

            # No background task running — spawn one
            _background_state = state_row.state
            _background_started_at = _time.time()
            _background_future = _bg_executor.submit(handler, now, state_row, cfg)
            logger.info("[bg] spawned background task for %s", state_row.state)
            sm.touch_heartbeat()
            return

    # --- Inline execution for short handlers (idle, monitor, holiday) ---
    try:
        next_state = handler(now, state_row, cfg)
    except Exception:
        tb = traceback.format_exc()
        logger.exception("dispatcher handler %s threw", state_row.state)
        sm.transition_to(
            STATE_IDLE,
            trade_date=state_row.trade_date,
            last_error=tb,
            note=f"handler {state_row.state!r} crashed",
        )
        return

    if next_state is not None and next_state != state_row.state:
        sm.transition_to(
            next_state,
            trade_date=_trade_date_for_transition(now, state_row, next_state),
        )
    else:
        sm.touch_heartbeat()


# ---------------------------------------------------------------------------
# Handlers — each returns next_state (str) or None to stay without updating
# ---------------------------------------------------------------------------

def handle_idle(now: datetime, state_row: sm.StateRow, cfg: dict) -> Optional[str]:
    today = now.strftime("%Y-%m-%d")
    # Dry-run E2E: fully bypass the wall-clock precheck gate AND the
    # already-ran-today guard so the cycle can be re-fired on demand for
    # testing. The live pipeline still respects both.
    if cfg.get("dry_run_e2e"):
        return STATE_PRECHECK
    if not sm.at_or_after(now, cfg.get("precheck_time", "08:10")):
        return None
    if sm.has_completed_today(today, "precheck"):
        return None
    return STATE_PRECHECK


def _build_dry_run_plan(cfg: dict, trade_date: str) -> dict:
    """Construct a synthetic trade plan for dry-run E2E.

    Skips the screener and the multi-agent analysis stack entirely — the
    debate path is well-exercised by the live pipeline and is not what the
    dry-run is trying to validate. Levels come from the ``dry_run_plan``
    config so they can be tuned without code changes.
    """
    ticker = cfg.get("dry_run_ticker", "RELIANCE.NS")
    plan_cfg = cfg.get("dry_run_plan", {}) or {}
    return {
        "ticker": ticker,
        "date": trade_date,
        "rating": "Buy",
        "entry_zone_low": float(plan_cfg.get("entry_zone_low", 1400.0)),
        "entry_zone_high": float(plan_cfg.get("entry_zone_high", 1410.0)),
        "stop_loss": float(plan_cfg.get("stop_loss", 1385.0)),
        "target_1": float(plan_cfg.get("target_1", 1440.0)),
        "target_2": float(plan_cfg.get("target_2", 1465.0)),
        "confidence_score": int(plan_cfg.get("confidence_score", 7)),
        "position_size_pct": float(plan_cfg.get("position_size_pct", 15.0)),
        "skip_rule_time": cfg.get("execution_window_end", "15:15"),
        "thesis": "[DRY RUN] synthetic plan — screener and agents skipped",
        "is_dry_run": True,
    }


def handle_precheck(now: datetime, state_row: sm.StateRow, cfg: dict) -> Optional[str]:
    """One-shot: screener + multi-agent analysis.

    In dry-run E2E mode the screener and the entire LangGraph debate stack
    are skipped — a synthetic plan is built from ``dry_run_plan`` config
    and persisted so the rest of the pipeline (execution, monitor, capital
    log, analysis) can be exercised end-to-end without any LLM or live
    market data.
    """
    trade_date = state_row.trade_date or now.strftime("%Y-%m-%d")
    runtime = _get_runtime(trade_date)
    runtime.clear()

    dry_run = bool(cfg.get("dry_run_e2e", False))

    _tg(
        "Precheck started",
        trade_date=trade_date,
        mode="DRY RUN" if dry_run else "live",
    )

    if dry_run:
        from tradingagents.web.database import get_trade_plans, insert_trade_plan
        plan = _build_dry_run_plan(cfg, trade_date)
        try:
            existing = [
                p for p in get_trade_plans(trade_date)
                if p.get("ticker") == plan["ticker"] and p.get("rating") == "Buy"
            ]
            if existing:
                logger.info(
                    "[precheck] DRY RUN: plan already exists for %s today; skipping insert",
                    plan["ticker"],
                )
            else:
                insert_trade_plan(plan)
        except Exception as e:
            logger.warning("[precheck] DRY RUN: failed to persist mock plan: %s", e)
        runtime["plans"] = [plan]
        logger.info(
            "[precheck] DRY RUN: synthesized plan for %s — entry %.2f-%.2f SL %.2f T1 %.2f T2 %.2f",
            plan["ticker"], plan["entry_zone_low"], plan["entry_zone_high"],
            plan["stop_loss"], plan["target_1"], plan["target_2"],
        )
        _tg(
            "Precheck complete",
            body=f"1 synthetic plan ready (dry run)",
            ticker=plan["ticker"],
            rating=plan["rating"],
            entry=f"{plan['entry_zone_low']}–{plan['entry_zone_high']}",
            stop_loss=plan["stop_loss"],
            target_1=plan["target_1"],
            target_2=plan["target_2"],
            confidence=f"{plan['confidence_score']}/10",
        )
        return STATE_WAITING

    from run_pipeline import run_analysis_phase, run_screener

    top_n = int(cfg.get("top_k_positions", 3))
    logger.info("[precheck] running screener (top_n=%d)", top_n)
    top_stocks = run_screener(top_n=top_n)
    if not top_stocks:
        logger.info("[precheck] no stocks passed screening; back to idle")
        _tg("Precheck: no candidates", body="Screener returned 0 stocks — back to idle.")
        return STATE_IDLE

    plans = run_analysis_phase(top_stocks, date=trade_date)
    runtime["plans"] = plans
    if not plans:
        logger.info("[precheck] no actionable plans; back to idle")
        _tg(
            "Precheck: no actionable plans",
            body=f"{len(top_stocks)} screened, 0 actionable — back to idle.",
        )
        return STATE_IDLE

    logger.info("[precheck] %d actionable plans ready", len(plans))
    plan_lines = "\n".join(
        f"• <b>{p.get('ticker')}</b> {p.get('rating', '?')} "
        f"conf {p.get('confidence_score', '?')}/10  "
        f"entry {p.get('entry_zone_low')}–{p.get('entry_zone_high')} "
        f"SL {p.get('stop_loss')} T1 {p.get('target_1')} T2 {p.get('target_2')}"
        for p in plans
    )
    tg.notify_html(
        f"<b>Precheck complete</b> · {len(plans)} actionable plan(s)\n{plan_lines}"
    )
    return STATE_WAITING


def handle_waiting(now: datetime, state_row: sm.StateRow, cfg: dict) -> Optional[str]:
    # Dry-run E2E: skip the wall-clock execution gate so orders are placed
    # immediately after precheck completes.
    if not cfg.get("dry_run_e2e") and not sm.at_or_after(now, cfg.get("execution_time", "09:30")):
        return None

    trade_date = state_row.trade_date or now.strftime("%Y-%m-%d")
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

    from tradingagents.web import capital_service
    capital_service.init_day(trade_date, starting_capital)
    _snapshot_capital(trade_date, paper_trader, trigger="day_init")
    _tg(
        "Day initialised",
        trade_date=trade_date,
        start_capital=tg.fmt_money(starting_capital),
        plans_pending=len(plans),
    )

    order_ids = run_execution_phase(plans, paper_trader)
    runtime["order_ids"] = order_ids
    _snapshot_capital(trade_date, paper_trader, trigger="orders_placed")

    # Per-plan outcome notification. We map every plan to either a successful
    # order (placed, with the actual qty/levels persisted by OrderManager) or
    # a rejection (with the reason verbatim from paper_trader._reject).
    placed_orders_by_ticker = {
        o.ticker: o for o in paper_trader.order_manager.orders.values()
    }
    placed_lines, rejected_lines = [], []
    for plan in plans:
        t = plan.get("ticker")
        if t in placed_orders_by_ticker:
            o = placed_orders_by_ticker[t]
            placed_lines.append(
                f"• <b>{t}</b> qty {o.quantity} entry {o.entry_zone_low}–{o.entry_zone_high} "
                f"SL {o.stop_loss} T1 {o.target_1} T2 {o.target_2}"
            )
        else:
            reason = paper_trader.last_rejection_reason.get(t, "unknown")
            rejected_lines.append(f"• <b>{t}</b> — {reason}")

    if placed_lines or rejected_lines:
        chunks = [f"<b>Order placement</b> · {trade_date}"]
        if placed_lines:
            chunks.append(f"\n<b>Placed ({len(placed_lines)}):</b>\n"
                          + "\n".join(placed_lines))
        if rejected_lines:
            chunks.append(f"\n<b>Rejected ({len(rejected_lines)}):</b>\n"
                          + "\n".join(rejected_lines))
        tg.notify_html("\n".join(chunks))

    if not order_ids:
        logger.info("[waiting] no orders placed; skipping monitor")
        _tg(
            "No orders placed — skipping monitor",
            body="All plans were rejected; going straight to analysis.",
        )
        return STATE_ANALYSIS

    logger.info("[waiting] %d orders placed", len(order_ids))
    return STATE_MONITOR


def _snapshot_capital(
    trade_date: str,
    paper_trader,
    *,
    trigger: str = "tick",
    current_prices: Optional[dict] = None,
) -> None:
    """Persist the current capital buckets to ``daily_metrics`` and append a
    row to ``capital_log`` so the UI can render the intraday history table.

    ``trigger`` should describe what caused the snapshot (e.g.
    ``order_placed``, ``monitor_tick``, ``day_init``, ``day_finalized``).
    """
    try:
        from tradingagents.web import capital_service
        state = paper_trader.get_capital_state(current_prices=current_prices)
        capital_service.snapshot(
            trade_date,
            free_cash=state["free_cash"],
            invested=state["invested"],
            pending_reserved=state["pending_reserved"],
            daily_pnl=state["realized_pnl"],
        )
        capital_service.log_snapshot(
            trade_date,
            start_capital=state["start_capital"],
            current_value=state["current_value"],
            free_cash=state["free_cash"],
            invested=state["invested"],
            pending_reserved=state["pending_reserved"],
            realized_pnl=state["realized_pnl"],
            unrealized_pnl=state.get("unrealized_pnl", 0.0),
            open_positions_count=state.get("open_positions_count", 0),
            trigger=trigger,
        )
    except Exception as e:
        logger.warning("[capital] snapshot failed for %s: %s", trade_date, e)


def _paper_trader_has_work(paper_trader: Any) -> bool:
    return bool(
        paper_trader.order_manager.get_open_orders()
        or paper_trader.position_tracker.open_positions
    )


def _restore_paper_trader_from_db(trade_date: str, cfg: dict):
    """Restore monitor state after process restart.

    Pending orders are not persisted separately, so we recreate them from Buy
    trade plans for tickers that do not already have a position row today.
    """
    from tradingagents.execution.order_manager import Order, OrderStatus
    from tradingagents.execution.paper_trader import PaperTrader
    from tradingagents.execution.position_tracker import Position
    from tradingagents.web.database import get_latest_capital, get_positions, get_trade_plans

    starting_capital = get_latest_capital(
        default=cfg.get("initial_capital", 20000),
        before_date=trade_date,
    )
    paper_trader = PaperTrader(initial_capital=starting_capital)

    from tradingagents.web import capital_service
    capital_service.init_day(trade_date, starting_capital)

    day_positions = [p for p in get_positions() if p.get("date") == trade_date]
    tickers_with_position = {p.get("ticker") for p in day_positions}
    open_pos = [p for p in day_positions if p.get("status") == "open"]

    for p in open_pos:
        entry_price = p.get("entry_price", 0)
        qty = p.get("quantity", 0)
        order_id = str(p.get("id", p["ticker"]))
        pos = Position(
            ticker=p["ticker"],
            quantity=qty,
            entry_price=entry_price,
            stop_loss=p.get("stop_loss", 0),
            target_1=p.get("target_1", 0),
            target_2=p.get("target_2", 0) or p.get("target_1", 0),
            order_id=order_id,
        )
        capital_used = entry_price * qty
        paper_trader.position_tracker.add_position(pos, capital_used)
        paper_trader.order_manager.orders[order_id] = Order(
            ticker=p["ticker"],
            side="buy",
            quantity=qty,
            entry_zone_low=entry_price,
            entry_zone_high=entry_price,
            stop_loss=p.get("stop_loss", 0),
            target_1=p.get("target_1", 0),
            target_2=p.get("target_2", 0) or p.get("target_1", 0),
            order_id=order_id,
            status=OrderStatus.FILLED,
            filled_price=entry_price,
            filled_qty=qty,
        )

    # Only re-place orders for tickers that (a) have no position today (open or
    # closed) AND (b) don't already have a pending order in this paper_trader.
    # Without this guard, expired orders get re-placed on restart.
    plans = [p for p in get_trade_plans(trade_date) if p.get("rating") == "Buy"]
    existing_order_tickers = {o.ticker for o in paper_trader.order_manager.orders.values()}
    restored_orders = 0
    for plan in plans:
        ticker = plan.get("ticker")
        if ticker in tickers_with_position or ticker in existing_order_tickers:
            continue
        oid = paper_trader.place_trade_plan(plan)
        if oid:
            existing_order_tickers.add(ticker)
            restored_orders += 1

    logger.info(
        "[monitor] restored %d open position(s) and %d pending order(s) from DB",
        len(open_pos), restored_orders,
    )
    return paper_trader


def handle_monitor(now: datetime, state_row: sm.StateRow, cfg: dict) -> Optional[str]:
    """Runs monitor.tick() throttled by dispatcher_monitor_interval_sec.

    In dry-run E2E mode the wall-clock gate and throttle are bypassed and
    the entire scripted price sequence is consumed inside a single
    dispatcher tick. Any pending orders that never filled and any open
    positions that didn't hit an exit are force-closed so the pipeline can
    advance to analysis on the same tick.
    """
    dry_run = bool(cfg.get("dry_run_e2e", False))
    monitor_interval = 60 if dry_run else int(cfg.get("dispatcher_monitor_interval_sec", 600))
    trade_date = state_row.trade_date or now.strftime("%Y-%m-%d")

    # Hard exit must run through MarketMonitor so open positions are closed
    # before the state advances to analysis. It bypasses the poll throttle.
    hard_exit_due = sm.at_or_after(now, cfg.get("hard_exit_time", "15:15"))

    global _last_monitor_tick_at
    if not dry_run:
        # Don't poll until the execution window is open
        if not hard_exit_due and not sm.at_or_after(now, cfg.get("execution_window_start", "10:30")):
            return None
        # Throttle: skip if not enough time elapsed since last poll
        now_ts = _time.time()
        elapsed = (now_ts - _last_monitor_tick_at) if _last_monitor_tick_at is not None else monitor_interval
        if not hard_exit_due and elapsed < monitor_interval:
            logger.debug("[monitor] throttled: %.0fs elapsed < %ds interval", elapsed, monitor_interval)
            return None

    runtime = _get_runtime(trade_date)
    paper_trader = runtime.get("paper_trader")
    if paper_trader is None:
        paper_trader = _restore_paper_trader_from_db(trade_date, cfg)
        runtime["paper_trader"] = paper_trader

    if not _paper_trader_has_work(paper_trader):
        logger.info("[monitor] no pending orders or open positions for %s; jumping to analysis", trade_date)
        return STATE_ANALYSIS

    # Reuse MarketMonitor across ticks so dry-run price sequence and other
    # monitor-local state can advance.
    from tradingagents.execution.risk_manager import RiskThresholds
    from tradingagents.pipeline.market_monitor import MarketMonitor

    monitor = runtime.get("monitor")
    if monitor is None or monitor.paper_trader is not paper_trader:
        risk = RiskThresholds(
            breakeven_trigger_pct=float(cfg.get("breakeven_trigger_pct", 0.5)),
            trail_trigger_pct=float(cfg.get("trail_trigger_pct", 1.0)),
            trail_lock_pct=float(cfg.get("trail_lock_pct", 0.3)),
        )
        news_monitor = None
        if cfg.get("news_check_enabled", True) and not dry_run:
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
    else:
        monitor.poll_interval = monitor_interval

    if dry_run:
        return _run_dry_run_monitor(now, trade_date, paper_trader, monitor, cfg)

    window_closed = monitor.tick(now)
    _last_monitor_tick_at = _time.time()
    _snapshot_capital(
        trade_date,
        paper_trader,
        trigger="hard_exit" if window_closed else "monitor_tick",
        current_prices=getattr(monitor, "_last_prices", None) or None,
    )
    _tg_monitor_snapshot(
        trade_date,
        paper_trader,
        getattr(monitor, "_last_prices", None) or None,
        hard_exit=window_closed,
    )
    if window_closed:
        if paper_trader.position_tracker.open_positions:
            logger.warning(
                "[monitor] window closed but positions/orders remain; staying in monitor"
            )
            return None
        logger.info("[monitor] execution window closed; transitioning to analysis")
        return STATE_ANALYSIS

    return None


def _tg_monitor_snapshot(
    trade_date: str,
    paper_trader,
    prices: Optional[dict],
    *,
    hard_exit: bool = False,
) -> None:
    """Format one monitor-tick state into a Telegram update."""
    try:
        state = paper_trader.get_capital_state(current_prices=prices)
    except Exception as e:
        logger.debug("[telegram] snapshot read failed: %s", e)
        return
    title = "Hard exit fired" if hard_exit else "Monitor tick"
    _tg(
        title,
        trade_date=trade_date,
        current_value=tg.fmt_money(state.get("current_value")),
        free_cash=tg.fmt_money(state.get("free_cash")),
        invested=tg.fmt_money(state.get("invested")),
        pending=tg.fmt_money(state.get("pending_reserved")),
        realized_pnl=tg.fmt_pnl(state.get("realized_pnl")),
        unrealized_pnl=tg.fmt_pnl(state.get("unrealized_pnl")),
        open_positions=state.get("open_positions_count"),
    )


def _run_dry_run_monitor(
    now: datetime,
    trade_date: str,
    paper_trader,
    monitor,
    cfg: dict,
) -> Optional[str]:
    """Drive the entire monitor phase to completion inside one dispatcher tick.

    Loops monitor.tick() while there is work (pending orders or open
    positions). On the way out, cancels any pending orders that never
    filled and force-exits any open positions at the last seen price so
    the pipeline can deterministically advance to analysis.
    """
    global _last_monitor_tick_at
    from tradingagents.execution.order_manager import OrderStatus

    seq = cfg.get("dry_run_price_sequence", [1400.0]) or [1400.0]
    max_iters = max(2 * len(seq), 8)

    iterations = 0
    while iterations < max_iters and _paper_trader_has_work(paper_trader):
        monitor.tick(now)
        iterations += 1
        _snapshot_capital(
            trade_date,
            paper_trader,
            trigger="monitor_tick",
            current_prices=getattr(monitor, "_last_prices", None) or None,
        )
    logger.info(
        "[monitor] DRY RUN: consumed %d internal tick(s); open=%d pending=%d",
        iterations,
        len(paper_trader.position_tracker.open_positions),
        len(paper_trader.order_manager.get_open_orders()),
    )
    _tg_monitor_snapshot(
        trade_date,
        paper_trader,
        getattr(monitor, "_last_prices", None) or None,
    )

    # Cancel pending orders that never filled in the scripted sequence so
    # _paper_trader_has_work() returns False below.
    for order in list(paper_trader.order_manager.get_open_orders()):
        order.status = OrderStatus.CANCELLED
        logger.info("[monitor] DRY RUN: cancelled unfilled pending order %s", order.order_id)

    # Force-exit any remaining open positions (e.g. T2 runner that the
    # scripted price sequence never reached).
    if paper_trader.position_tracker.open_positions:
        last_prices = getattr(monitor, "_last_prices", None) or {}
        # Synthesize a final price for tickers that never had a price reading.
        for ticker in list(paper_trader.position_tracker.open_positions.keys()):
            if ticker not in last_prices:
                pos = paper_trader.position_tracker.open_positions[ticker]
                last_prices[ticker] = pos.entry_price
        events = paper_trader.hard_exit_all(last_prices, now)
        for event in events:
            monitor._handle_event(event, now)
        _snapshot_capital(
            trade_date,
            paper_trader,
            trigger="dry_run_force_exit",
            current_prices=last_prices or None,
        )

    _last_monitor_tick_at = _time.time()
    return STATE_ANALYSIS


def handle_analysis(now: datetime, state_row: sm.StateRow, cfg: dict) -> Optional[str]:
    """One-shot: reporting + EOD reflection."""
    trade_date = state_row.trade_date or now.strftime("%Y-%m-%d")
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

    _snapshot_capital(trade_date, paper_trader, trigger="day_finalized")
    from tradingagents.web import capital_service
    capital_service.finalize_day(trade_date)

    # EOD report bundle: zip the day's reports/<DATE>/ tree (all agent
    # markdown + debates + decisions) and upload to Telegram as a single
    # archive. Gated by telegram_reports_enabled + telegram_reports_eod_zip.
    try:
        reports_dir = cfg.get("reports_dir", "")
        if reports_dir:
            tg.send_eod_reports_zip(trade_date, reports_dir)
    except Exception as e:
        logger.debug("[telegram] EOD zip send failed silently: %s", e)

    # End-of-day Telegram summary: start vs end capital, realized P&L, trade
    # count, win count. Pull the freshly-finalized row from daily_metrics so
    # the numbers match what tomorrow's `start_capital` will be.
    try:
        row = capital_service.get_today(trade_date) or {}
        metrics = paper_trader.get_state().get("metrics", {})
        start_c = row.get("start_capital")
        end_c = row.get("capital")
        pnl = row.get("daily_pnl") or 0.0
        ret_pct = (pnl / start_c * 100.0) if start_c else 0.0
        _tg(
            f"Day closed — {trade_date}",
            start_capital=tg.fmt_money(start_c),
            end_capital=tg.fmt_money(end_c),
            daily_pnl=f"{tg.fmt_pnl(pnl)} ({ret_pct:+.2f}%)",
            trades=metrics.get("total_trades"),
            wins=metrics.get("winning_trades"),
            win_rate=f"{metrics.get('win_rate', 0):.1f}%",
            max_drawdown=f"{metrics.get('max_drawdown_pct', 0):.2f}%",
        )
    except Exception as e:
        logger.debug("[telegram] EOD summary failed: %s", e)

    _clear_runtime()
    logger.info("[analysis] complete; back to idle")
    return STATE_IDLE


def handle_holiday(now: datetime, state_row: sm.StateRow, cfg: dict) -> Optional[str]:
    # Dry-run E2E: never stick in holiday — flip back to idle so the
    # synthetic cycle can fire regardless of the wall-clock calendar.
    if cfg.get("dry_run_e2e"):
        return STATE_IDLE
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
