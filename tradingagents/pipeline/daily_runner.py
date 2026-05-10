"""DEPRECATED: standalone APScheduler runner.

This module predates the cron-dispatcher state machine
([dispatcher.py](dispatcher.py)) and the DB-backed config service. It is
**no longer the production scheduler** — that role is now played by the
``BackgroundScheduler`` started in the FastAPI lifespan, which fires the
state-machine dispatcher.

Reasons it's outdated:
  - Hardcodes ``top_n=5`` / ``poll_interval=120`` instead of reading from
    the DB-backed app_config table.
  - Uses three fixed cron triggers (08:30 / 10:30 / 15:20) instead of the
    new 4-stage state machine (precheck / waiting / monitor / analysis)
    with dynamic intervals.
  - Calls the old ``run_monitoring_phase`` blocking loop instead of the
    new ``MarketMonitor.tick()`` pattern.
  - Doesn't read the persisted starting capital from the DB.

The ``run_once()`` helper is kept for offline testing of the full daily
cycle in one go (``python -m tradingagents.pipeline.daily_runner --once``)
but the scheduler entry-point should not be used in production. Run the
FastAPI process instead — its lifespan starts the dispatcher.

Original design (kept for reference):
    08:30  — Screen + Analyze
    10:30  — Place orders + start market monitor (blocks until 15:15)
    15:20  — Save daily metrics
"""

import argparse
import logging
import signal
import sys
from datetime import datetime
from typing import List

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from dotenv import load_dotenv

load_dotenv()

from tradingagents.dataflows.indian_market import is_market_open, IST
from tradingagents.web.database import init_db

logger = logging.getLogger(__name__)

# Module-level state shared between scheduled jobs within a single day.
_daily_state = {
    "plans": [],
    "paper_trader": None,
    "order_ids": [],
}


def _reset_daily_state():
    _daily_state["plans"] = []
    _daily_state["paper_trader"] = None
    _daily_state["order_ids"] = []


# ---------------------------------------------------------------------------
# Job 1: Screen + Analyze (08:30 IST)
# ---------------------------------------------------------------------------


def job_screen_and_analyze(top_n: int = 5):
    """Screen the universe and run multi-agent analysis."""
    _reset_daily_state()

    if not is_market_open():
        logger.info("[Job 1] Market closed today. Skipping.")
        return

    logger.info("[Job 1] Starting screen + analyze (top_n=%d)", top_n)

    from run_pipeline import run_screener, run_analysis_phase

    top_stocks = run_screener(top_n=top_n)
    if not top_stocks:
        logger.warning("[Job 1] No stocks passed screening.")
        return

    plans = run_analysis_phase(top_stocks)
    _daily_state["plans"] = plans
    logger.info("[Job 1] Done. %d actionable plans ready.", len(plans))


# ---------------------------------------------------------------------------
# Job 2: Execute + Monitor (10:30 IST)
# ---------------------------------------------------------------------------


def job_execute_and_monitor(poll_interval: int = 120):
    """Place orders and start market monitoring (blocks until 15:15)."""
    plans = _daily_state["plans"]

    if not plans:
        logger.info("[Job 2] No actionable plans. Skipping execution.")
        return

    logger.info("[Job 2] Placing %d orders and starting monitor.", len(plans))

    from tradingagents.execution.paper_trader import PaperTrader
    from run_pipeline import run_execution_phase, run_monitoring_phase

    paper_trader = PaperTrader()
    _daily_state["paper_trader"] = paper_trader

    order_ids = run_execution_phase(plans, paper_trader)
    _daily_state["order_ids"] = order_ids

    if not order_ids:
        logger.info("[Job 2] No orders placed. Skipping monitoring.")
        return

    # Blocks until 15:15 IST
    run_monitoring_phase(paper_trader, poll_interval=poll_interval)
    logger.info("[Job 2] Monitoring complete.")


# ---------------------------------------------------------------------------
# Job 3: Daily Reporting (15:20 IST)
# ---------------------------------------------------------------------------


def job_daily_report():
    """Save daily metrics after market close."""
    paper_trader = _daily_state["paper_trader"]

    if paper_trader is None:
        logger.info("[Job 3] No paper trader active today. Skipping report.")
        return

    logger.info("[Job 3] Saving daily metrics.")

    from run_pipeline import run_reporting_phase

    run_reporting_phase(paper_trader)
    logger.info("[Job 3] Daily report saved.")


# ---------------------------------------------------------------------------
# Scheduler Setup
# ---------------------------------------------------------------------------


def create_scheduler(top_n: int = 5, poll_interval: int = 120) -> BlockingScheduler:
    """Create and configure the APScheduler with IST-aware cron triggers."""
    scheduler = BlockingScheduler(timezone=IST)

    # 08:30 Mon-Fri: Screen + Analyze
    scheduler.add_job(
        job_screen_and_analyze,
        trigger=CronTrigger(day_of_week="mon-fri", hour=8, minute=30, timezone=IST),
        kwargs={"top_n": top_n},
        id="screen_and_analyze",
        name="Screen + Analyze",
        misfire_grace_time=1800,  # 30 min grace
    )

    # 10:30 Mon-Fri: Execute + Monitor
    scheduler.add_job(
        job_execute_and_monitor,
        trigger=CronTrigger(day_of_week="mon-fri", hour=10, minute=30, timezone=IST),
        kwargs={"poll_interval": poll_interval},
        id="execute_and_monitor",
        name="Execute + Monitor",
        misfire_grace_time=900,  # 15 min grace
    )

    # 15:20 Mon-Fri: Daily Report
    scheduler.add_job(
        job_daily_report,
        trigger=CronTrigger(day_of_week="mon-fri", hour=15, minute=20, timezone=IST),
        id="daily_report",
        name="Daily Report",
        misfire_grace_time=600,  # 10 min grace
    )

    return scheduler


def run_once(top_n: int = 5, poll_interval: int = 120):
    """Run all three jobs sequentially once (for testing)."""
    logger.info("=== Running all jobs once (test mode) ===")
    job_screen_and_analyze(top_n=top_n)
    job_execute_and_monitor(poll_interval=poll_interval)
    job_daily_report()
    logger.info("=== All jobs complete ===")


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    parser = argparse.ArgumentParser(description="Daily Trading Pipeline Scheduler")
    parser.add_argument("--top-n", type=int, default=5, help="Stocks to analyze (default: 5)")
    parser.add_argument("--poll-interval", type=int, default=120, help="Price poll interval in seconds")
    parser.add_argument("--once", action="store_true", help="Run all jobs once sequentially (testing)")
    args = parser.parse_args()

    init_db()

    if args.once:
        run_once(top_n=args.top_n, poll_interval=args.poll_interval)
        return

    scheduler = create_scheduler(top_n=args.top_n, poll_interval=args.poll_interval)

    # Graceful shutdown on SIGINT/SIGTERM
    def _shutdown(signum, frame):
        logger.info("Received signal %d, shutting down scheduler...", signum)
        scheduler.shutdown(wait=False)
        sys.exit(0)

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    logger.info("Daily runner started. Scheduled jobs (IST):")
    for job in scheduler.get_jobs():
        logger.info("  %s -> %s", job.name, job.trigger)
    logger.info("Waiting for next trigger...")

    scheduler.start()


if __name__ == "__main__":
    main()
