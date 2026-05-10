"""End-to-end intraday trading pipeline.

Connects: Screener -> Multi-Agent Analysis -> Paper Trading -> Market Monitoring -> Reporting.

Usage:
    source .venv/bin/activate
    python run_pipeline.py                  # Full pipeline (screen + analyze + trade + monitor)
    python run_pipeline.py --analyze-only   # Screen + analyze only (no execution/monitoring)
    python run_pipeline.py --top-n 3        # Analyze top 3 instead of 5
"""

import argparse
import logging
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import List, Optional

from dotenv import load_dotenv

load_dotenv()

from tradingagents.screener import NSE_MIDCAP_SMALLCAP_UNIVERSE, ScreenFilters, Ranker
from tradingagents.graph.trading_graph import TradingAgentsGraph
from tradingagents.execution.paper_trader import PaperTrader
from tradingagents.pipeline.plan_extractor import extract_trade_plan
from tradingagents.pipeline.market_monitor import MarketMonitor
from tradingagents.dataflows.indian_market import is_market_open, is_execution_window, IST
from tradingagents.web.database import (
    init_db,
    insert_trade_plan,
    insert_debate,
    insert_agent_report,
    insert_daily_metrics,
    get_latest_capital,
)
from tradingagents.default_config import DEFAULT_CONFIG  # noqa: F401  (fallback only)


def _runtime_config():
    """Fresh DB-backed config dict. Used by every place in this file that
    used to read ``DEFAULT_CONFIG`` directly. Falls back to the static dict
    if the DB is unreachable so the pipeline never crashes at startup."""
    try:
        from tradingagents.web.config_service import load_config
        return load_config()
    except Exception:
        return dict(DEFAULT_CONFIG)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Phase 1: Screening
# ---------------------------------------------------------------------------


def run_screener(top_n: int = 5) -> List[dict]:
    """Screen NSE universe and return top N ranked stocks."""
    logger.info("=== Phase 1: Screening %d stocks ===", len(NSE_MIDCAP_SMALLCAP_UNIVERSE[:80]))
    filters = ScreenFilters()
    results = filters.screen(NSE_MIDCAP_SMALLCAP_UNIVERSE[:80])
    passed = filters.get_passed(results)
    logger.info("Passed filters: %d stocks", len(passed))

    ranker = Ranker()
    ranked = ranker.rank(passed, top_n=top_n)
    logger.info("Top %d stocks selected:", len(ranked))
    for i, stock in enumerate(ranked, 1):
        logger.info(
            "  %d. %s - price=%.0f, vol=%.1fCr, ATR=%.1f%%, score=%.3f",
            i,
            stock["ticker"],
            stock.get("price", 0),
            stock.get("avg_volume_inr_crores", 0),
            stock.get("atr_pct", 0),
            stock.get("composite_score", 0),
        )
    return ranked


# ---------------------------------------------------------------------------
# Phase 2: Multi-Agent Analysis
# ---------------------------------------------------------------------------


def run_analysis_phase(
    top_stocks: List[dict],
    config: Optional[dict] = None,
    date: Optional[str] = None,
) -> List[dict]:
    """Run the multi-agent graph for each screened stock in parallel.

    Each stock gets its own thread and its own TradingAgentsGraph instance —
    the work is I/O-bound (LLM API + yfinance HTTP), so threads scale linearly.
    SQLite writes are safe via WAL mode (see init_db).

    ``date`` lets the caller pass a historical YYYY-MM-DD for dry-run analysis.
    When None, defaults to today's date in IST.

    Returns list of actionable trade plans (high-conviction Buy with levels).
    """
    n = len(top_stocks)
    logger.info("=== Phase 2: Analyzing %d stocks in parallel (%d threads) ===", n, n)
    date = date or datetime.now(IST).strftime("%Y-%m-%d")
    cfg = config or _runtime_config()

    all_plans: List[dict] = []
    completed = 0

    def analyze_one(stock: dict):
        ticker = stock["ticker"]
        graph = TradingAgentsGraph(debug=False, config=cfg)
        final_state, rating = graph.propagate(ticker, date)
        plan = extract_trade_plan(ticker, date, final_state, rating)
        _save_to_db(ticker, date, final_state, plan, rating)
        return ticker, plan, rating

    with ThreadPoolExecutor(max_workers=max(n, 1)) as ex:
        futures = {ex.submit(analyze_one, s): s["ticker"] for s in top_stocks}
        for fut in as_completed(futures):
            ticker = futures[fut]
            completed += 1
            try:
                t, plan, rating = fut.result()
                all_plans.append(plan)
                conf = plan.get("confidence_score") or 0
                logger.info("  [%d/%d] %s %s (conf=%d/10)",
                            completed, n, t, rating, conf)
            except Exception as e:
                logger.error("  [%d/%d] ✗ %s: FAILED: %s", completed, n, ticker, e, exc_info=True)

    # ─── Phase 2.5: Cross-stock allocator ─────────────────────────────────
    # Rank every analyzed plan by confidence × R:R, take top K, distribute
    # capital weighted by score. Force-promote the best Skip if everything
    # came back Skip — the desk always trades the best of the day.
    from tradingagents.pipeline.allocator import rank_and_allocate
    top_k = cfg.get("top_k_positions", 3)
    deploy_pct = cfg.get("deploy_pct_top_k", 70.0)
    cap = cfg.get("max_capital_per_stock_pct", 25.0)

    result = rank_and_allocate(
        all_plans, top_k=top_k, deploy_pct=deploy_pct, max_per_stock_pct=cap,
    )
    logger.info("=== Phase 2.5: Allocator ===")
    logger.info("  %s", result.summary)
    if result.promoted_from_skip:
        logger.info("  (force-best-of-N: best Skip promoted to half-size Buy)")
    for p in result.saved_only:
        logger.info("  ○ %s: ranked #%d — saved, not traded", p["ticker"], p["rank_position"])

    logger.info("Allocator picked %d of %d as actionable.", len(result.traded), len(all_plans))
    return result.traded


def _save_to_db(ticker: str, date: str, final_state: dict, plan: dict, rating: str):
    """Save analysis results to SQLite for the web dashboard, and write a
    full per-agent markdown report tree to ``reports_dir/<DATE>/<TICKER>/``."""
    # Persist the complete agent-team analysis as markdown — one folder per
    # ticker per day, regardless of Buy/Skip outcome. This is the audit trail
    # the user reviews pre-market the next day.
    try:
        from tradingagents.pipeline.report_writer import save_daily_analysis
        save_daily_analysis(
            final_state=final_state,
            ticker=ticker,
            date=date,
            reports_dir=_runtime_config().get("reports_dir"),
        )
    except Exception as e:
        logger.warning("Daily report save failed for %s: %s", ticker, e)

    # Save trade plan
    insert_trade_plan(plan)

    # Save agent reports
    for agent_type, key in [
        ("Market Analyst", "market_report"),
        ("Retail Sentiment", "sentiment_retail_report"),
        ("Institutional Sentiment", "sentiment_institutional_report"),
        ("Contrarian Sentiment", "sentiment_contrarian_report"),
        ("News Analyst", "news_report"),
        ("Fundamentals Analyst", "fundamentals_report"),
        ("Portfolio Manager", "final_trade_decision"),
    ]:
        report = final_state.get(key, "")
        if report:
            insert_agent_report({
                "date": date,
                "ticker": ticker,
                "agent_type": agent_type,
                "report": report,
            })

    # Save debate
    debate_state = final_state.get("investment_debate_state", {})
    if debate_state.get("bull_history") or debate_state.get("bear_history"):
        insert_debate({
            "date": date,
            "ticker": ticker,
            "round_num": 1,
            "bull_argument": debate_state.get("bull_history", ""),
            "bear_argument": debate_state.get("bear_history", ""),
            "verdict": debate_state.get("judge_decision", ""),
            "confidence": plan.get("confidence_score"),
        })


# ---------------------------------------------------------------------------
# Phase 3: Execution
# ---------------------------------------------------------------------------


def run_execution_phase(plans: List[dict], paper_trader: PaperTrader) -> List[str]:
    """Feed actionable trade plans into PaperTrader."""
    logger.info("=== Phase 3: Placing %d orders ===", len(plans))
    order_ids = []

    for plan in plans:
        oid = paper_trader.place_trade_plan(plan)
        if oid:
            order_ids.append(oid)
            logger.info(
                "  Order %s: %s entry=%.2f-%.2f SL=%.2f T1=%.2f",
                oid, plan["ticker"],
                plan.get("entry_zone_low", 0), plan.get("entry_zone_high", 0),
                plan.get("stop_loss", 0), plan.get("target_1", 0),
            )
        else:
            logger.warning("  Order rejected for %s", plan["ticker"])

    logger.info("Placed %d orders.", len(order_ids))
    return order_ids


# ---------------------------------------------------------------------------
# Phase 4: Market Monitoring
# ---------------------------------------------------------------------------


def run_monitoring_phase(
    paper_trader: PaperTrader,
    poll_interval: int = 600,
    config: Optional[dict] = None,
):
    """Start the market monitor (blocks until 15:15 IST)."""
    cfg = config or _runtime_config()
    logger.info("=== Phase 4: Market monitoring (poll every %ds) ===", poll_interval)

    # Optional: news-event monitor with fast K2.5 (thinking-disabled) classifier.
    news_monitor_obj = None
    if cfg.get("news_check_enabled", True):
        try:
            from tradingagents.llm_clients.fast_classifier import FastClassifier
            from tradingagents.pipeline.news_monitor import NewsMonitor
            news_monitor_obj = NewsMonitor(
                classifier=FastClassifier(),
                lookback_min=cfg.get("news_check_lookback_min", 60),
            )
            logger.info(
                "News monitor enabled (lookback %d min).",
                cfg.get("news_check_lookback_min", 60),
            )
        except Exception as e:
            logger.warning("News monitor disabled: %s", e)

    monitor = MarketMonitor(
        paper_trader,
        poll_interval_sec=poll_interval,
        news_monitor=news_monitor_obj,
    )
    monitor.run()


# ---------------------------------------------------------------------------
# Phase 5: Reporting
# ---------------------------------------------------------------------------


def run_eod_reflection(paper_trader: PaperTrader, date: str):
    """Phase 5.5 — news-grounded post-mortem per closed trade.

    Wraps ``tradingagents.pipeline.eod_reflection.run_eod_reflection_sweep``
    with config-aware defaults and a memory-log instance constructed from
    ``DEFAULT_CONFIG``. The same FastClassifier used by the news monitor
    handles the reflection LLM calls — Kimi K2.5 thinking-disabled, ~1-2s
    per closed trade, so the whole sweep typically takes <10s.
    """
    try:
        from tradingagents.agents.utils.memory import TradingMemoryLog
        from tradingagents.llm_clients.fast_classifier import FastClassifier
        from tradingagents.pipeline.eod_reflection import run_eod_reflection_sweep
    except Exception as e:
        logger.warning("EOD reflection skipped — import error: %s", e)
        return

    logger.info("=== Phase 5.5: EOD reflection sweep ===")
    try:
        cfg = _runtime_config()
        memory_log = TradingMemoryLog(cfg)
        classifier = FastClassifier()
        results = run_eod_reflection_sweep(
            paper_trader=paper_trader,
            memory_log=memory_log,
            classifier=classifier,
            date=date,
            window_start=cfg.get("eod_news_window_start", "09:00"),
            window_end=cfg.get("eod_news_window_end", "15:30"),
        )
        logger.info("EOD reflection: %d trade(s) reflected.", len(results))
        for r in results:
            logger.info("  %s: %s", r.ticker, r.plan_adherence)
    except Exception as e:
        logger.warning("EOD reflection failed: %s", e, exc_info=True)


def run_reporting_phase(paper_trader: PaperTrader, date: Optional[str] = None):
    """Save daily metrics to SQLite."""
    logger.info("=== Phase 5: Daily reporting ===")
    metrics = paper_trader.get_state()["metrics"]
    date = date or datetime.now(IST).strftime("%Y-%m-%d")

    insert_daily_metrics({
        "date": date,
        "capital": metrics["current_capital"],
        "daily_pnl": metrics["daily_pnl"],
        "daily_return_pct": metrics["total_return_pct"],
        "total_trades": metrics["total_trades"],
        "win_rate": metrics["win_rate"],
        "max_drawdown_pct": metrics["max_drawdown_pct"],
        "notes": f"Analyzed {metrics['total_trades']} trades, {metrics['winning_trades']} wins",
    })

    logger.info("  Capital: Rs.%.0f (%.2f%% return)", metrics["current_capital"], metrics["total_return_pct"])
    logger.info("  Daily P&L: Rs.%.2f", metrics["daily_pnl"])
    logger.info("  Win rate: %.1f%% (%d trades)", metrics["win_rate"], metrics["total_trades"])
    logger.info("Pipeline complete for %s.", date)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def _validate_date(s: str) -> str:
    """Argparse type for --date: must be YYYY-MM-DD."""
    try:
        datetime.strptime(s, "%Y-%m-%d")
    except ValueError:
        raise argparse.ArgumentTypeError(f"date must be YYYY-MM-DD (got {s!r})")
    return s


def main():
    parser = argparse.ArgumentParser(description="Intraday Trading Pipeline")
    parser.add_argument("--top-n", type=int, default=5, help="Number of stocks to analyze (default: 5)")
    parser.add_argument("--analyze-only", action="store_true", help="Run screener + analysis only, no execution")
    parser.add_argument("--skip-market-check", action="store_true", help="Skip market open check (for testing)")
    parser.add_argument("--poll-interval", type=int, default=600, help="Price poll interval in seconds (default: 600 = 10 min)")
    parser.add_argument("--rich", action="store_true", help="Use rich terminal display (same as `tradingagents pipeline`)")
    parser.add_argument("--date", type=_validate_date, default=None,
                        help="Analysis date YYYY-MM-DD for dry-run on a historical day (default: today IST). "
                             "Auto-implies --analyze-only and --skip-market-check.")
    args = parser.parse_args()

    # When a historical --date is supplied we are dry-running: skip the market-hours
    # gate and skip live execution/monitoring, since the trading day has passed.
    if args.date:
        args.skip_market_check = True
        args.analyze_only = True

    if args.rich:
        from cli.main import pipeline as cli_pipeline
        cli_pipeline(
            top_n=args.top_n,
            analyze_only=args.analyze_only,
            skip_market_check=args.skip_market_check,
            poll_interval=args.poll_interval,
            date=args.date,
        )
        return

    init_db()

    if not args.skip_market_check and not is_market_open():
        logger.info("Market closed today (holiday or weekend). Exiting.")
        sys.exit(0)

    # Phase 1: Screen
    top_stocks = run_screener(top_n=args.top_n)
    if not top_stocks:
        logger.warning("No stocks passed screening. Exiting.")
        sys.exit(0)

    # Phase 2: Analyze (always parallel)
    plans = run_analysis_phase(top_stocks, date=args.date)

    if args.analyze_only:
        logger.info("--analyze-only: skipping execution and monitoring.")
        return

    if not plans:
        logger.info("No actionable plans (no Buy/Overweight). Exiting.")
        return

    # Phase 3: Execute. Capital persists across days: today's starting balance
    # is yesterday's end-of-day capital from the daily_metrics table. First-ever
    # run falls back to DEFAULT_CONFIG["initial_capital"].
    starting_capital = get_latest_capital(
        default=_runtime_config()["initial_capital"],
        before_date=args.date,
    )
    logger.info("=== Phase 3: Starting capital ₹%.2f ===", starting_capital)
    paper_trader = PaperTrader(initial_capital=starting_capital)
    order_ids = run_execution_phase(plans, paper_trader)

    if not order_ids:
        logger.info("No orders placed. Exiting.")
        return

    # Phase 4: Monitor (blocks until market close)
    now = datetime.now(IST)
    if is_execution_window(now):
        run_monitoring_phase(paper_trader, poll_interval=args.poll_interval, config=_runtime_config())
    else:
        logger.info("Outside execution window (%s). Skipping monitoring.", now.strftime("%H:%M"))

    # Phase 5: Report
    run_reporting_phase(paper_trader, date=args.date)

    # Phase 5.5: EOD reflection sweep — news-grounded post-mortem per closed
    # trade, written back to the memory log so tomorrow's PM sees today's
    # lessons via past_context injection.
    if _runtime_config().get("eod_reflection_enabled", True):
        run_eod_reflection(paper_trader, date=args.date or datetime.now(IST).strftime("%Y-%m-%d"))


if __name__ == "__main__":
    main()
