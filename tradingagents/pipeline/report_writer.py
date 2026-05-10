"""Daily analysis report writer.

Persists every agent's output for every analyzed ticker in a date-first
markdown directory structure:

    ~/.tradingagents/reports/
      └── 2026-05-09/
          ├── RVNL.NS/
          │   ├── complete_report.md          ← top-level aggregated view
          │   ├── 1_analysts/
          │   │   ├── market.md
          │   │   ├── news.md
          │   │   ├── fundamentals.md
          │   │   ├── sentiment_retail.md
          │   │   ├── sentiment_institutional.md
          │   │   └── sentiment_contrarian.md
          │   ├── 2_research/
          │   │   ├── bull.md
          │   │   ├── bear.md
          │   │   └── manager.md
          │   ├── 3_trading/
          │   │   └── trader.md
          │   ├── 4_risk/
          │   │   ├── aggressive.md
          │   │   ├── conservative.md
          │   │   └── neutral.md
          │   └── 5_portfolio/
          │       └── decision.md
          ├── BEL.NS/
          │   └── ... (same structure)
          └── NHPC.NS/
              └── ...

Why this layout:
  - **Date-first** so you can ``ls ~/.tradingagents/reports/2026-05-09/`` and
    instantly see every stock that was analyzed today.
  - **Per-agent files** so you can ``cat .../2_research/bull.md`` to read the
    bull argument in isolation without scrolling through a 50-page report.
  - **complete_report.md** at the top level so you can ``cat`` one file and
    get the full pre-market briefing.

This runs after every Phase 2 analysis (regardless of Buy/Skip) so the user
has an audit trail of what the agents thought before any trades were placed.
"""

from __future__ import annotations

import datetime as _dt
import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


# Section keys → (subdir, filename, display name) used to drive both the
# per-file writes and the complete_report.md aggregation. Adding a new section
# is a single tuple here.
_ANALYST_FILES = [
    ("market_report", "1_analysts/market.md", "Market Analyst"),
    ("news_report", "1_analysts/news.md", "News Analyst"),
    ("fundamentals_report", "1_analysts/fundamentals.md", "Fundamentals Analyst"),
    ("sentiment_retail_report", "1_analysts/sentiment_retail.md", "Retail Sentiment"),
    ("sentiment_institutional_report", "1_analysts/sentiment_institutional.md", "Institutional Sentiment"),
    ("sentiment_contrarian_report", "1_analysts/sentiment_contrarian.md", "Contrarian Sentiment"),
    # Legacy single-sentiment field, kept for backward compat with older flows.
    ("sentiment_report", "1_analysts/sentiment.md", "Social Sentiment"),
]


def _safe_ticker(ticker: str) -> str:
    """Sanitize a ticker for use as a path component.

    Tickers like ``RVNL.NS`` are fine, but defensively reject anything with
    path separators or parent-dir traversal.
    """
    bad = {"/", "\\", "..", ":"}
    if any(b in ticker for b in bad):
        raise ValueError(f"Unsafe ticker for path component: {ticker!r}")
    return ticker


def save_daily_analysis(
    final_state: dict,
    ticker: str,
    date: str,
    reports_dir: Optional[str] = None,
) -> Path:
    """Write the full multi-agent analysis to disk in date/ticker hierarchy.

    Args:
      final_state: the dict returned by ``TradingAgentsGraph.propagate``,
        containing all per-agent reports and debate state.
      ticker: e.g. "RVNL.NS". Sanitized to prevent path traversal.
      date: trade date in YYYY-MM-DD form.
      reports_dir: base directory. Defaults to ~/.tradingagents/reports.

    Returns the path to the ticker's directory (so the caller can log /
    display it).
    """
    base = Path(reports_dir).expanduser() if reports_dir else Path("~/.tradingagents/reports").expanduser()
    safe = _safe_ticker(ticker)
    ticker_dir = base / date / safe
    ticker_dir.mkdir(parents=True, exist_ok=True)

    sections: list[str] = []

    # 1) Analyst reports — one file per analyst that has output.
    analyst_parts: list[tuple[str, str]] = []
    for key, rel_path, display in _ANALYST_FILES:
        text = final_state.get(key)
        if not text:
            continue
        target = ticker_dir / rel_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        analyst_parts.append((display, text))
    if analyst_parts:
        body = "\n\n".join(f"### {name}\n\n{txt}" for name, txt in analyst_parts)
        sections.append(f"## I. Analyst Team Reports\n\n{body}")

    # 2) Research team debate (Bull / Bear / Manager).
    debate = final_state.get("investment_debate_state") or {}
    research_parts: list[tuple[str, str]] = []
    for key, fname, display in (
        ("bull_history", "bull.md", "Bull Researcher"),
        ("bear_history", "bear.md", "Bear Researcher"),
        ("judge_decision", "manager.md", "Research Manager"),
    ):
        text = (debate.get(key) or "").strip()
        if not text:
            continue
        target = ticker_dir / "2_research" / fname
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        research_parts.append((display, text))
    if research_parts:
        body = "\n\n".join(f"### {name}\n\n{txt}" for name, txt in research_parts)
        sections.append(f"## II. Research Team Decision\n\n{body}")

    # 3) Trader plan.
    trader_text = (final_state.get("trader_investment_plan") or "").strip()
    if trader_text:
        target = ticker_dir / "3_trading" / "trader.md"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(trader_text, encoding="utf-8")
        sections.append(f"## III. Trading Team Plan\n\n### Trader\n\n{trader_text}")

    # 4) Risk debate (Aggressive / Conservative / Neutral).
    risk = final_state.get("risk_debate_state") or {}
    risk_parts: list[tuple[str, str]] = []
    for key, fname, display in (
        ("aggressive_history", "aggressive.md", "Aggressive Analyst"),
        ("conservative_history", "conservative.md", "Conservative Analyst"),
        ("neutral_history", "neutral.md", "Neutral Analyst"),
    ):
        text = (risk.get(key) or "").strip()
        if not text:
            continue
        target = ticker_dir / "4_risk" / fname
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        risk_parts.append((display, text))
    if risk_parts:
        body = "\n\n".join(f"### {name}\n\n{txt}" for name, txt in risk_parts)
        sections.append(f"## IV. Risk Management Team Debate\n\n{body}")

    # 5) Portfolio Manager final decision.
    pm_text = (
        final_state.get("final_trade_decision")
        or risk.get("judge_decision")
        or ""
    ).strip()
    if pm_text:
        target = ticker_dir / "5_portfolio" / "decision.md"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(pm_text, encoding="utf-8")
        sections.append(f"## V. Portfolio Manager Decision\n\n### Portfolio Manager\n\n{pm_text}")

    # Aggregated complete_report.md with a header.
    header = (
        f"# Trading Analysis Report: {ticker}\n\n"
        f"**Trade date**: {date}\n"
        f"**Generated**: {_dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
        "---\n\n"
    )
    complete = ticker_dir / "complete_report.md"
    complete.write_text(header + "\n\n".join(sections), encoding="utf-8")

    logger.info(
        "Daily report saved: %s (%d analyst, %d research, %d risk parts)",
        complete, len(analyst_parts), len(research_parts), len(risk_parts),
    )
    return ticker_dir
