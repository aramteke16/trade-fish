"""Trader: turns the Research Manager's intraday entry plan into a concrete transaction proposal."""

from __future__ import annotations

import logging
import functools

logger = logging.getLogger(__name__)

from langchain_core.messages import AIMessage

from tradingagents.agents.schemas import TraderProposal, render_trader_proposal
from tradingagents.agents.utils.agent_utils import (
    build_instrument_context,
    get_intraday_context,
)
from tradingagents.agents.utils.structured import (
    bind_structured,
    invoke_structured_or_freetext,
)


_TRADER_FREETEXT_FORMAT = """---
IMPORTANT — your response MUST use these exact section headers (the system parses them by name):

**Action**: Buy
**Reasoning**: (2-4 sentences)
**Entry Zone**: ₹{entry_low} - ₹{entry_high}
**Stop Loss**: {sl_price}
**Target 1**: {t1_price}
**Target 2**: {t2_price}
**Confidence**: {n}/10
**Skip Rule**: (time cutoff, e.g. Skip if not filled by 11:30 IST)

FINAL TRANSACTION PROPOSAL: **BUY**  ← replace with **SKIP** if skipping

If action is Skip: include Action, Reasoning, Confidence — omit price levels."""


def create_trader(llm):
    structured_llm = bind_structured(llm, TraderProposal, "Trader")

    def trader_node(state, name):
        company_name = state["company_of_interest"]
        instrument_context = build_instrument_context(company_name)
        intraday_context = get_intraday_context()
        investment_plan = state["investment_plan"]

        messages = [
            {
                "role": "system",
                "content": (
                    "You are an intraday trader on an Indian-equities desk, executing same-day "
                    "round-trips on NSE. You take a research analyst's plan and turn it into a "
                    "concrete order: entry zone, stop-loss, targets, position size, and a skip-rule "
                    "cutoff. You only output BUY (place the order today) or SKIP (no trade). You do "
                    "not hold overnight. You do not short. You execute long, intraday, square-off "
                    "by 15:15 — and you only Buy when every level is concrete and R:R ≥ 1.5 to T1."
                ),
            },
            {
                "role": "user",
                "content": (
                    f"{instrument_context}\n\n"
                    f"{intraday_context}\n\n"
                    f"---\n\n"
                    f"**Research Manager's intraday entry plan for {company_name}:**\n"
                    f"{investment_plan}\n\n"
                    f"---\n\n"
                    f"**Your job:** Turn the plan above into a concrete intraday order. Respond with "
                    f"BUY or SKIP plus the full set of price levels and sizing. Be specific — give "
                    f"actual numbers in INR (₹), not ranges like 'around ₹500'. Reasoning must be "
                    f"2-4 sentences and tie back to the analysts' reports and the research plan.\n\n"
                    f"**If BUY**, fill in every numeric field:\n"
                    f"- entry_zone_low / entry_zone_high (a tight range, typically 0.3-0.8% wide)\n"
                    f"- stop_loss (1.0-1.5x ATR(14) below entry, or below VWAP / opening-range low — "
                    f"whichever is tighter)\n"
                    f"- target_1 (R:R ≥ 1.5 to entry — partial 50% exit here)\n"
                    f"- target_2 (R:R ≥ 2.5 to entry — final exit here)\n"
                    f"- position_size_pct (capped at 25% of capital; smaller if SL is wide)\n"
                    f"- confidence_score (1-10; if < 6 you must rate SKIP, not BUY)\n"
                    f"- skip_rule (e.g. 'Skip if entry zone not filled by 11:30 IST' — always include "
                    f"a time-of-day cutoff)\n\n"
                    f"**If SKIP**, briefly state which specific intraday rule fails (R:R, missing "
                    f"levels, contradictory bias, low confidence, event risk, etc.). You do not "
                    f"need to fill price levels for a Skip.\n\n"
                    f"Default to SKIP when the research plan is vague, the levels would not survive "
                    f"the 1.5%-of-capital risk budget, or the setup contradicts intraday bias filters "
                    f"(VWAP, RSI, index direction)."
                ),
            },
        ]

        logger.info("[Trader] %s: invoking Trader", company_name)

        trader_plan = invoke_structured_or_freetext(
            structured_llm,
            llm,
            messages,
            render_trader_proposal,
            "Trader",
            freetext_suffix=_TRADER_FREETEXT_FORMAT,
        )

        plan_preview = " | ".join(
            line.strip() for line in trader_plan.splitlines()[:4] if line.strip()
        )
        logger.info("[Trader] %s: proposal → %s", company_name, plan_preview)

        return {
            "messages": [AIMessage(content=trader_plan)],
            "trader_investment_plan": trader_plan,
            "sender": name,
        }

    return functools.partial(trader_node, name="Trader")
