"""End-of-day reflection sweep — Phase 5.5 of the intraday pipeline.

After 15:15 IST (or after the analyze-only flow finishes), iterate every
trade closed today and produce a news-grounded reflection per trade. The
reflection is written back to ``TradingMemoryLog`` via the existing
``batch_update_with_outcomes`` plumbing — which means tomorrow's Portfolio
Manager prompt automatically sees today's lessons via the
``get_past_context`` injection that's already wired into PM's prompt.

Why per-trade and not per-decision: an entry that hit T1+T2 vs an entry that
hit SL produce different lessons. The Reflector needs the actual outcome,
not just the original plan. Plan-vs-reality attribution is what makes the
lesson useful.

Why grounded in news: technical signals fail or work *because* of a catalyst.
"My RSI 55 entry got stopped out" is useless without knowing that an FII
sell-off hit the sector at noon. Pulling intraday news for the trade window
gives the LLM the missing context.

Cost: ~3-5 closed trades/day × ~2K input tokens × ~1 LLM call = trivial
(<₹3 per EOD sweep with kimi-k2.5 thinking-disabled).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import List, Optional

from tradingagents.agents.utils.memory import TradingMemoryLog
from tradingagents.dataflows.indian_market import IST
from tradingagents.execution.paper_trader import PaperTrader
from tradingagents.execution.position_tracker import Position
from tradingagents.llm_clients.fast_classifier import FastClassifier

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Plan-adherence (deterministic): "what happened vs what was planned?"
# ---------------------------------------------------------------------------


def classify_plan_adherence(closed: Position) -> str:
    """Map (exit_reason, original SL vs final SL) to a one-line summary.

    The trailing-stop ladder mutates ``stop_loss`` in place, so by the time we
    inspect a closed trade we can compare ``stop_loss`` (final) against the
    original SL implied by entry × (1 - max_loss%/100). For our purposes the
    cleaner heuristic is: did final SL >= entry? If so, the ladder ratcheted
    SL into profit territory, which colors the meaning of a 'stop_loss' exit.
    """
    reason = (closed.exit_reason or "").lower()
    in_profit = closed.pnl is not None and closed.pnl > 0
    sl_at_or_above_entry = closed.stop_loss >= closed.entry_price

    if reason == "target_2":
        return "Plan executed cleanly: both targets hit, runner caught the full move."
    if reason == "target_1":
        return "T1 hit (full close due to small qty); thesis worked but no T2 ride."
    if reason == "stop_loss" and sl_at_or_above_entry and in_profit:
        return "Trailing-stop locked in gain on a fade — would have been a full SL loss without the ratchet."
    if reason == "stop_loss" and sl_at_or_above_entry:
        return "Trailing-stop hit at break-even after a brief pop — flat exit, no real damage."
    if reason == "stop_loss":
        return "Stopped out at original SL: thesis failed; setup did not develop."
    if reason == "hard_exit":
        if in_profit:
            return "Held to 15:15 hard exit with mild gain — neither thesis nor invalidation played out by close."
        return "Held to 15:15 hard exit at loss — position drifted against us without triggering SL."
    if reason == "news_exit":
        return "Mid-day news catalyst forced exit — see news context for the trigger."
    return f"Closed for reason '{reason}' — non-standard exit path."


# ---------------------------------------------------------------------------
# News-grounded reflection (one LLM call per closed trade)
# ---------------------------------------------------------------------------


_REFLECTION_SYSTEM = (
    "You are a senior intraday trading analyst writing a one-paragraph "
    "post-mortem on a closed same-day position. The desk runs analysis on "
    "the screened universe each morning, picks top-3, and exits everything "
    "by 15:15 IST. Your reflection will be re-injected into tomorrow's "
    "Portfolio Manager prompt, so every word must be useful for future "
    "decisions. Avoid generic advice; be specific to *this* trade, *this* "
    "ticker, *this* day's news."
)


def _build_reflection_prompt(
    closed: Position,
    plan_text: str,
    plan_adherence: str,
    news_text: str,
) -> str:
    pnl_str = (
        f"₹{closed.pnl:+.2f} ({closed.pnl_pct:+.2f}%)"
        if closed.pnl is not None and closed.pnl_pct is not None
        else "n/a"
    )
    return (
        f"Trade post-mortem for {closed.ticker}\n\n"
        f"Original plan:\n{plan_text or '(plan text not available)'}\n\n"
        f"Outcome:\n"
        f"- Entry: ₹{closed.entry_price:.2f} × {closed.quantity} shares\n"
        f"- Exit: ₹{closed.exit_price:.2f} (reason: {closed.exit_reason})\n"
        f"- Realized P&L: {pnl_str}\n"
        f"- Final SL at exit: ₹{closed.stop_loss:.2f} "
        f"(initial SL implied by plan; trailing ladder may have raised it)\n\n"
        f"Plan adherence:\n{plan_adherence}\n\n"
        f"News during the trade day:\n{news_text or '(no news pulled)'}\n\n"
        "Write 3-5 sentences in plain prose covering, in order:\n"
        "1. Which intraday signal *actually* drove the move (VWAP, RSI, ATR, "
        "news catalyst, FII flow, sector rotation)?\n"
        "2. Which assumption from the original plan held or failed, with "
        "specific evidence?\n"
        "3. ONE concrete lesson for similar setups on this ticker. Start "
        "the lesson with 'Lesson:' so it's grep-able."
    )


def build_reflection(
    closed: Position,
    plan_text: str,
    plan_adherence: str,
    news_text: str,
    classifier: FastClassifier,
) -> str:
    """Single LLM call producing a 3-5 sentence reflection paragraph."""
    prompt = _build_reflection_prompt(closed, plan_text, plan_adherence, news_text)
    raw = classifier.classify(prompt, system=_REFLECTION_SYSTEM)
    return (raw or plan_adherence).strip()


# ---------------------------------------------------------------------------
# News fetch for the trade day window
# ---------------------------------------------------------------------------


def _fetch_intraday_news(
    ticker: str, date: str,
    window_start: str = "09:00", window_end: str = "15:30",
) -> str:
    """Pull yfinance news for the trade day, filter to the IST trading-day
    window, return a compact bulleted block. Empty string on failure."""
    try:
        from tradingagents.dataflows.yfinance_news import _extract_article_data
        import yfinance as yf

        articles = yf.Ticker(ticker).news[:30]
        if not articles:
            return ""

        target_date = datetime.strptime(date, "%Y-%m-%d").date()
        sh, sm = map(int, window_start.split(":"))
        eh, em = map(int, window_end.split(":"))

        lines: List[str] = []
        for art in articles:
            data = _extract_article_data(art)
            pub = data.get("pub_date")
            if not pub:
                continue
            pub_naive = pub.replace(tzinfo=None) if pub.tzinfo else pub
            if pub_naive.date() != target_date:
                continue
            t = pub_naive.time()
            from datetime import time as dtime
            if not (dtime(sh, sm) <= t <= dtime(eh, em)):
                continue
            line = f"- [{t.strftime('%H:%M')}] {data['title']}"
            if data.get("summary"):
                line += f" — {data['summary'][:200]}"
            lines.append(line)

        return "\n".join(lines)
    except Exception as e:
        logger.debug("Intraday news fetch failed for %s on %s: %s", ticker, date, e)
        return ""


# ---------------------------------------------------------------------------
# Sweep
# ---------------------------------------------------------------------------


@dataclass
class ReflectionResult:
    ticker: str
    date: str
    pnl: float
    pnl_pct: float
    holding_min: int
    plan_adherence: str
    reflection: str


def run_eod_reflection_sweep(
    paper_trader: PaperTrader,
    memory_log: TradingMemoryLog,
    classifier: FastClassifier,
    date: str,
    nifty_return_pct: Optional[float] = None,
    window_start: str = "09:00",
    window_end: str = "15:30",
) -> List[ReflectionResult]:
    """For each trade closed today, generate a news-grounded reflection and
    write it back into the memory log via the existing batch update.

    Args:
      paper_trader: source of ``closed_trades`` for the day.
      memory_log: target log; pending entries are matched and updated.
      classifier: FastClassifier for the LLM reflection calls.
      date: YYYY-MM-DD trading day. Only trades whose ``closed_at`` falls on
        this date are reflected; older trades are skipped.
      nifty_return_pct: optional same-day Nifty50 return for alpha computation.
        When None, alpha is recorded as the raw return (alpha == raw).
      window_start, window_end: news filter window in HH:MM IST.

    Returns the list of ReflectionResult entries (one per reflected trade).
    """
    target_date = datetime.strptime(date, "%Y-%m-%d").date()

    # Filter closed trades to today.
    todays = [
        c for c in paper_trader.position_tracker.closed_trades
        if c.closed_at is not None and c.closed_at.date() == target_date
    ]
    if not todays:
        logger.info("EOD reflection sweep: no closed trades for %s.", date)
        return []

    # Pull plan text from memory log so the LLM sees what the agents originally
    # decided. Same source as the past_context injection — keeps the loop self-
    # contained on the markdown log.
    pending_by_ticker: dict[str, dict] = {}
    for e in memory_log.get_pending_entries():
        if e.get("date") == date:
            pending_by_ticker[e["ticker"]] = e

    # Tickers the user has flagged "exclude from feedback" via the UI —
    # one-off market events shouldn't poison the long-term memory log.
    excluded_tickers: set[str] = set()
    try:
        from tradingagents.web.database import get_conn
        conn = get_conn()
        try:
            rows = conn.execute(
                "SELECT ticker FROM trade_plans WHERE date = ? AND exclude_from_feedback = 1",
                (date,),
            ).fetchall()
            excluded_tickers = {r["ticker"] for r in rows}
        finally:
            conn.close()
    except Exception as e:
        logger.debug("exclude_from_feedback lookup failed: %s", e)

    results: List[ReflectionResult] = []
    updates: List[dict] = []

    for closed in todays:
        if closed.ticker in excluded_tickers:
            logger.info(
                "EOD reflection: skipping %s (flagged exclude_from_feedback)",
                closed.ticker,
            )
            continue
        plan_entry = pending_by_ticker.get(closed.ticker, {})
        plan_text = plan_entry.get("decision", "")
        plan_adherence = classify_plan_adherence(closed)
        news_text = _fetch_intraday_news(
            closed.ticker, date, window_start, window_end,
        )
        reflection = build_reflection(
            closed, plan_text, plan_adherence, news_text, classifier,
        )

        holding_min = 0
        if closed.opened_at and closed.closed_at:
            opened = closed.opened_at
            closed_at = closed.closed_at
            # Strip tz for naive arithmetic if present.
            opened_naive = opened.replace(tzinfo=None) if opened.tzinfo else opened
            closed_naive = closed_at.replace(tzinfo=None) if closed_at.tzinfo else closed_at
            holding_min = int(max(0, (closed_naive - opened_naive).total_seconds() / 60))

        raw_return_frac = (closed.pnl_pct or 0) / 100.0
        alpha = (
            raw_return_frac - (nifty_return_pct or 0) / 100.0
            if nifty_return_pct is not None
            else raw_return_frac
        )

        # batch_update_with_outcomes expects holding_days (int). For intraday
        # we pass 0 — the tag stores "0d" meaning "same-day". The minute
        # granularity is captured inside the reflection prose where it matters.
        updates.append({
            "ticker": closed.ticker,
            "trade_date": date,
            "raw_return": raw_return_frac,
            "alpha_return": alpha,
            "holding_days": 0,
            "reflection": reflection,
        })
        results.append(ReflectionResult(
            ticker=closed.ticker,
            date=date,
            pnl=closed.pnl or 0.0,
            pnl_pct=closed.pnl_pct or 0.0,
            holding_min=holding_min,
            plan_adherence=plan_adherence,
            reflection=reflection,
        ))
        logger.info(
            "[EOD] %s: %s — wrote %d-char reflection",
            closed.ticker, plan_adherence, len(reflection),
        )

    if updates:
        memory_log.batch_update_with_outcomes(updates)
        logger.info("EOD reflection sweep: %d trades reflected, log updated.", len(updates))
    return results
