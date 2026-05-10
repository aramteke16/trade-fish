"""Mid-day news-event monitor for invested intraday positions.

Runs inside :class:`MarketMonitor._poll_cycle`. For each open position:

  1. Fetch fresh news via yfinance (structured records with pub_date + URL).
  2. Filter to articles published in the last ``lookback_min`` minutes.
  3. Deduplicate against the per-ticker ``seen_news_ids`` set so the same
     headline is never classified twice.
  4. If anything new appears, run a single Kimi K2.5 (thinking-disabled)
     classifier call asking HOLD vs EXIT.
  5. EXIT actions are returned to the caller, which forces a position
     close at the current price.

Cost: ~1.2s per ticker per poll, dominated by the LLM call. With 3 invested
stocks parallelized, total mid-day check overhead is ~2 seconds — 0.3% of
the 10-minute polling window.

Industry rationale: this is the standard "event-driven exit" pattern from
event-driven investing literature (cf. Wikipedia: Event-driven investing).
We do NOT re-run the full 14-agent debate every poll — that would be 30x
the cost for 5% additional information capture. The classifier handles the
sharp tail of material news; everything else is left to the trailing-stop
ladder + 15:15 hard exit.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import List, Optional

import yfinance as yf

from tradingagents.llm_clients.fast_classifier import FastClassifier

logger = logging.getLogger(__name__)


# Classifier prompt — kept terse; ~150 tokens total. The system prompt sets
# the role; the user prompt embeds position state + headlines.
_SYSTEM_PROMPT = (
    "You are an intraday risk officer for an Indian-equities long-only desk. "
    "You see a position that is already open and a list of fresh news headlines. "
    "Your only job is to decide if the desk should EXIT the position now or "
    "continue HOLDING through the existing stop-loss / target levels.\n\n"
    "EXIT only on these material catalysts:\n"
    "- Trading halt or suspension on the stock\n"
    "- Regulator action (SEBI/RBI investigation, penalty, license issue)\n"
    "- Fraud allegation, accounting concern, or insider-deal news\n"
    "- Major analyst downgrade with target cut\n"
    "- Sector breakdown >2% on bad macro/regulatory news\n"
    "- Earnings miss leak or unscheduled disclosure\n"
    "- Promoter pledge / large stake sale\n\n"
    "HOLD on everything else: routine broker notes, sector momentum, "
    "general market chatter, mid-cap activity, or generic positive news. "
    "The trailing-stop ladder will manage normal volatility — don't override it."
)


@dataclass
class NewsArticle:
    """Lightweight article record for classifier prompt + dedup."""

    article_id: str
    title: str
    summary: str
    publisher: str
    pub_date: Optional[datetime]


@dataclass
class NewsAction:
    """Output of the classifier for a single ticker on a single poll."""

    ticker: str
    decision: str       # "HOLD" or "EXIT"
    reason: str
    headlines: List[str] = field(default_factory=list)


class NewsMonitor:
    """Per-ticker news watcher with dedup, attached to a MarketMonitor."""

    def __init__(
        self,
        classifier: Optional[FastClassifier] = None,
        lookback_min: int = 60,
    ):
        self.classifier = classifier
        self.lookback_min = lookback_min
        # ticker -> set of article_ids already classified.
        self._seen: dict[str, set[str]] = {}

    # -- Public API ---------------------------------------------------------

    def evaluate_position(
        self,
        ticker: str,
        entry_price: float,
        current_price: float,
        stop_loss: float,
        target_1: float,
        now: datetime,
    ) -> Optional[NewsAction]:
        """Fetch fresh news for ticker, classify, return action or None.

        Returns None when (a) no fresh news, or (b) classifier disabled, or
        (c) classifier failed silently. The caller treats None as "no action".
        """
        if self.classifier is None:
            return None

        fresh = self._fetch_fresh_news(ticker, now)
        if not fresh:
            return None

        # Build classifier prompt.
        unrealized_pct = ((current_price - entry_price) / entry_price) * 100
        headlines_block = "\n".join(
            f"- [{a.pub_date.strftime('%H:%M') if a.pub_date else '??:??'}] "
            f"{a.title} ({a.publisher})"
            + (f" — {a.summary[:200]}" if a.summary else "")
            for a in fresh
        )
        prompt = (
            f"Ticker: {ticker}\n"
            f"Entry: ₹{entry_price:.2f}\n"
            f"Current: ₹{current_price:.2f} ({unrealized_pct:+.2f}%)\n"
            f"Stop-loss: ₹{stop_loss:.2f}\n"
            f"Target 1: ₹{target_1:.2f}\n"
            f"Time now: {now.strftime('%H:%M IST')}\n\n"
            f"Fresh news (last {self.lookback_min} min):\n{headlines_block}\n\n"
            "Output exactly two lines:\n"
            "DECISION: HOLD or EXIT\n"
            "REASON: <one sentence>"
        )

        raw = self.classifier.classify(prompt, system=_SYSTEM_PROMPT)
        if not raw:
            return None

        decision, reason = self._parse_response(raw)
        action = NewsAction(
            ticker=ticker,
            decision=decision,
            reason=reason,
            headlines=[a.title for a in fresh],
        )

        # Mark these articles seen *only after* a successful classifier call,
        # so a transient API failure leaves them unseen for the next poll.
        self._seen.setdefault(ticker, set()).update(a.article_id for a in fresh)

        logger.info(
            "[news] %s: %s — %s (%d fresh headline%s)",
            ticker, decision, reason, len(fresh),
            "" if len(fresh) == 1 else "s",
        )
        return action

    # -- Internals ----------------------------------------------------------

    def _fetch_fresh_news(self, ticker: str, now: datetime) -> List[NewsArticle]:
        """Pull yfinance news for ticker, return only articles within
        lookback window AND not previously classified."""
        try:
            raw = yf.Ticker(ticker).news[:20]
        except Exception as e:
            logger.debug("yfinance news fetch failed for %s: %s", ticker, e)
            return []

        cutoff = now - timedelta(minutes=self.lookback_min)
        seen = self._seen.get(ticker, set())
        # Make `now` timezone-naive comparisons safe by converting fetched
        # pub_date to naive in the same tz as `now` (we're given IST).
        cutoff_naive = cutoff.replace(tzinfo=None) if cutoff.tzinfo else cutoff

        fresh: List[NewsArticle] = []
        for art in raw:
            content = art.get("content") if isinstance(art, dict) else None
            if not content:
                continue
            pub_date_str = content.get("pubDate", "")
            pub_date = None
            if pub_date_str:
                try:
                    pub_date = datetime.fromisoformat(pub_date_str.replace("Z", "+00:00"))
                except (ValueError, AttributeError):
                    pass
            if pub_date is None:
                # No timestamp = can't filter; skip rather than over-include.
                continue

            pub_naive = pub_date.replace(tzinfo=None)
            if pub_naive < cutoff_naive:
                continue

            url_obj = content.get("canonicalUrl") or content.get("clickThroughUrl") or {}
            link = url_obj.get("url", "")
            title = content.get("title", "").strip()
            article_id = link or title  # URL preferred, title fallback
            if not article_id or article_id in seen:
                continue

            fresh.append(NewsArticle(
                article_id=article_id,
                title=title,
                summary=content.get("summary", "").strip()[:300],
                publisher=(content.get("provider") or {}).get("displayName", "Unknown"),
                pub_date=pub_date,
            ))

        return fresh

    @staticmethod
    def _parse_response(raw: str) -> tuple[str, str]:
        """Pull DECISION + REASON out of the classifier's response.

        Defensive: any unexpected shape collapses to HOLD with the raw text
        as the reason. We never fail closed (would block the polling loop).
        """
        decision = "HOLD"
        reason = raw[:200]
        for line in raw.splitlines():
            line = line.strip()
            if line.upper().startswith("DECISION:"):
                tail = line.split(":", 1)[1].strip().upper()
                if tail.startswith("EXIT"):
                    decision = "EXIT"
                elif tail.startswith("HOLD"):
                    decision = "HOLD"
            elif line.upper().startswith("REASON:"):
                reason = line.split(":", 1)[1].strip() or reason
        return decision, reason
