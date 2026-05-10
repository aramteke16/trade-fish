"""Pydantic schemas used by agents that produce structured output.

The framework's primary artifact is still prose: each agent's natural-language
reasoning is what users read in the saved markdown reports and what the
downstream agents read as context.  Structured output is layered onto the
three decision-making agents (Research Manager, Trader, Portfolio Manager)
so that:

- Their outputs follow consistent section headers across runs and providers
- Each provider's native structured-output mode is used (json_schema for
  OpenAI/xAI, response_schema for Gemini, tool-use for Anthropic)
- Schema field descriptions become the model's output instructions, freeing
  the prompt body to focus on context and the rating-scale guidance
- A render helper turns the parsed Pydantic instance back into the same
  markdown shape the rest of the system already consumes, so display,
  memory log, and saved reports keep working unchanged
"""

from __future__ import annotations

from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Shared decision type — same-day intraday, long-only
# ---------------------------------------------------------------------------


class TradingDecision(str, Enum):
    """2-tier decision shared by Research Manager, Trader, and Portfolio Manager.

    The pipeline trades long-only same-day intraday on NSE India: every
    decision collapses to "Buy this stock today" or "Skip it today". There is
    no shorting and no overnight holding, so Hold/Sell/Overweight/Underweight
    add no signal — they all collapse to Skip downstream anyway.
    """

    BUY = "Buy"
    SKIP = "Skip"


# ---------------------------------------------------------------------------
# Research Manager
# ---------------------------------------------------------------------------


class ResearchPlan(BaseModel):
    """Structured intraday entry plan produced by the Research Manager.

    Hand-off to the Trader: the recommendation pins today's go/no-go, the
    rationale captures which side of the bull/bear debate carried the
    argument, and the strategic actions translate that into concrete
    instructions the trader can execute against.
    """

    recommendation: TradingDecision = Field(
        description=(
            "The intraday recommendation. Exactly one of Buy / Skip. "
            "Buy = today is tradeable as an intraday long. Skip = the stock "
            "cannot be safely traded today (no parseable levels, halted, "
            "scheduled major event). The desk ranks every analyzed name and "
            "trades the top 3 — default to Buy and let the trader / portfolio "
            "manager calibrate confidence; Skip is reserved for outright "
            "unsafe situations, not for 'meh' setups."
        ),
    )
    rationale: str = Field(
        description=(
            "Conversational summary of the key points from both sides of the "
            "debate, ending with which arguments led to the recommendation. "
            "Speak naturally, as if to a teammate."
        ),
    )
    strategic_actions: str = Field(
        description=(
            "Concrete steps for the trader to implement the recommendation, "
            "including position sizing guidance for an intraday round-trip."
        ),
    )


def render_research_plan(plan: ResearchPlan) -> str:
    """Render a ResearchPlan to markdown for storage and the trader's prompt context."""
    return "\n".join([
        f"**Recommendation**: {plan.recommendation.value}",
        "",
        f"**Rationale**: {plan.rationale}",
        "",
        f"**Strategic Actions**: {plan.strategic_actions}",
    ])


# ---------------------------------------------------------------------------
# Trader
# ---------------------------------------------------------------------------


class TraderProposal(BaseModel):
    """Structured transaction proposal produced by the Trader.

    The trader reads the Research Manager's investment plan and the analyst
    reports, then turns them into a concrete intraday transaction: whether
    to enter today, the reasoning that justifies it, and the practical
    levels for entry, stop-loss, and sizing.
    """

    action: TradingDecision = Field(
        description="The intraday decision. Exactly one of Buy / Skip.",
    )
    reasoning: str = Field(
        description=(
            "The case for this action, anchored in the analysts' reports and "
            "the research plan. Two to four sentences."
        ),
    )
    entry_price: Optional[float] = Field(
        default=None,
        description="Optional entry price target in the instrument's quote currency.",
    )
    entry_zone_low: Optional[float] = Field(
        default=None,
        description="Lower bound of the entry zone (buy range).",
    )
    entry_zone_high: Optional[float] = Field(
        default=None,
        description="Upper bound of the entry zone (buy range).",
    )
    stop_loss: Optional[float] = Field(
        default=None,
        description="Optional stop-loss price in the instrument's quote currency.",
    )
    target_1: Optional[float] = Field(
        default=None,
        description="First profit target (partial exit 50%).",
    )
    target_2: Optional[float] = Field(
        default=None,
        description="Second profit target (remaining 50% exit).",
    )
    position_sizing: Optional[str] = Field(
        default=None,
        description="Optional sizing guidance, e.g. '5% of portfolio'.",
    )
    position_size_pct: Optional[float] = Field(
        default=None,
        description="Position size as percentage of capital (e.g. 25.0 for 25%).",
    )
    confidence_score: Optional[int] = Field(
        default=None,
        ge=1,
        le=10,
        description="Confidence in this trade from 1 (low) to 10 (high).",
    )
    skip_rule: Optional[str] = Field(
        default=None,
        description="Condition under which to skip this trade, e.g. 'Skip if not in entry zone by 11:30 AM'.",
    )


def render_trader_proposal(proposal: TraderProposal) -> str:
    """Render a TraderProposal to markdown.

    The trailing ``FINAL TRANSACTION PROPOSAL: **BUY/SKIP**`` line is
    preserved as the analyst stop-signal phrase used by the graph.
    """
    parts = [
        f"**Action**: {proposal.action.value}",
        "",
        f"**Reasoning**: {proposal.reasoning}",
    ]
    if proposal.entry_price is not None:
        parts.extend(["", f"**Entry Price**: {proposal.entry_price}"])
    if proposal.entry_zone_low is not None and proposal.entry_zone_high is not None:
        parts.extend(["", f"**Entry Zone**: ₹{proposal.entry_zone_low} - ₹{proposal.entry_zone_high}"])
    if proposal.stop_loss is not None:
        parts.extend(["", f"**Stop Loss**: {proposal.stop_loss}"])
    if proposal.target_1 is not None:
        parts.extend(["", f"**Target 1**: {proposal.target_1}"])
    if proposal.target_2 is not None:
        parts.extend(["", f"**Target 2**: {proposal.target_2}"])
    if proposal.position_sizing:
        parts.extend(["", f"**Position Sizing**: {proposal.position_sizing}"])
    if proposal.position_size_pct is not None:
        parts.extend(["", f"**Position Size %**: {proposal.position_size_pct}%"])
    if proposal.confidence_score is not None:
        parts.extend(["", f"**Confidence**: {proposal.confidence_score}/10"])
    if proposal.skip_rule:
        parts.extend(["", f"**Skip Rule**: {proposal.skip_rule}"])
    parts.extend([
        "",
        f"FINAL TRANSACTION PROPOSAL: **{proposal.action.value.upper()}**",
    ])
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Portfolio Manager
# ---------------------------------------------------------------------------


class PortfolioDecision(BaseModel):
    """Structured output produced by the Portfolio Manager.

    The model fills every field as part of its primary LLM call; no separate
    extraction pass is required. Field descriptions double as the model's
    output instructions, so the prompt body only needs to convey context and
    the decision-scale guidance.
    """

    rating: TradingDecision = Field(
        description=(
            "The final intraday decision. Exactly one of Buy / Skip. "
            "Buy = today is tradeable on the analyst evidence. Skip = the "
            "stock cannot be safely traded today (event risk, no parseable "
            "levels, halted, mid-session suspension). The desk runs the "
            "analysis on N stocks each day and ranks them; your confidence "
            "score drives the rank, not Skip vs Buy. Default to Buy and let "
            "confidence reflect setup quality — Skip is reserved for outright "
            "unsafe situations."
        ),
    )
    executive_summary: str = Field(
        description=(
            "A concise action plan covering entry strategy, position sizing, "
            "key risk levels, and time-of-day cutoffs. Two to four sentences."
        ),
    )
    investment_thesis: str = Field(
        description=(
            "Detailed reasoning anchored in specific evidence from the analysts' "
            "debate. If prior lessons are referenced in the prompt context, "
            "incorporate them; otherwise rely solely on the current analysis."
        ),
    )
    price_target: Optional[float] = Field(
        default=None,
        description="Optional target price in the instrument's quote currency.",
    )
    time_horizon: Optional[str] = Field(
        default=None,
        description="Recommended holding window within the day, e.g. 'Exit by 14:30 if T1 not hit'.",
    )
    entry_zone_low: Optional[float] = Field(
        default=None,
        description="Lower bound of the entry zone (buy range).",
    )
    entry_zone_high: Optional[float] = Field(
        default=None,
        description="Upper bound of the entry zone (buy range).",
    )
    target_1: Optional[float] = Field(
        default=None,
        description="First profit target (partial exit 50%).",
    )
    target_2: Optional[float] = Field(
        default=None,
        description="Second profit target (remaining 50% exit).",
    )
    stop_loss: Optional[float] = Field(
        default=None,
        description="Stop-loss price in the instrument's quote currency.",
    )
    confidence_score: Optional[int] = Field(
        default=None,
        ge=1,
        le=10,
        description=(
            "Honest 1-10 rating of this setup's intraday quality. The desk "
            "ranks all analyzed stocks by confidence × R:R and trades the "
            "top 3 — your number drives where this stock lands in the rank. "
            "Use 8-9 for crystal-clear setups, 5-6 for decent-but-not-perfect, "
            "3-4 for weak. Be honest: spreading every stock at 5/10 makes the "
            "rank meaningless."
        ),
    )
    position_size_pct: Optional[float] = Field(
        default=None,
        description=(
            "Suggested position size as percentage of capital. The allocator "
            "may override this when distributing capital across the top 3."
        ),
    )
    skip_rule: Optional[str] = Field(
        default=None,
        description="Time-of-day cutoff for entry, e.g. 'Skip if not in entry zone by 11:30 AM'.",
    )


def render_pm_decision(decision: PortfolioDecision) -> str:
    """Render a PortfolioDecision back to the markdown shape the rest of the system expects.

    Memory log, CLI display, and saved report files all read this markdown,
    so the rendered output preserves the exact section headers (``**Rating**``,
    ``**Executive Summary**``, ``**Investment Thesis**``) that downstream
    parsers and the report writers already handle.
    """
    parts = [
        f"**Rating**: {decision.rating.value}",
        "",
        f"**Executive Summary**: {decision.executive_summary}",
        "",
        f"**Investment Thesis**: {decision.investment_thesis}",
    ]
    if decision.price_target is not None:
        parts.extend(["", f"**Price Target**: {decision.price_target}"])
    if decision.time_horizon:
        parts.extend(["", f"**Time Horizon**: {decision.time_horizon}"])
    if decision.entry_zone_low is not None and decision.entry_zone_high is not None:
        parts.extend(["", f"**Entry Zone**: ₹{decision.entry_zone_low} - ₹{decision.entry_zone_high}"])
    if decision.stop_loss is not None:
        parts.extend(["", f"**Stop Loss**: {decision.stop_loss}"])
    if decision.target_1 is not None:
        parts.extend(["", f"**Target 1**: {decision.target_1}"])
    if decision.target_2 is not None:
        parts.extend(["", f"**Target 2**: {decision.target_2}"])
    if decision.confidence_score is not None:
        parts.extend(["", f"**Confidence**: {decision.confidence_score}/10"])
    if decision.position_size_pct is not None:
        parts.extend(["", f"**Position Size %**: {decision.position_size_pct}%"])
    if decision.skip_rule:
        parts.extend(["", f"**Skip Rule**: {decision.skip_rule}"])
    return "\n".join(parts)
