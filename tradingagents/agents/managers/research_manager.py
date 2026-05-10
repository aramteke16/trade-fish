"""Research Manager: turns the bull/bear debate into a structured intraday entry plan for the trader."""

from __future__ import annotations

from tradingagents.agents.schemas import ResearchPlan, render_research_plan
from tradingagents.agents.utils.agent_utils import (
    build_instrument_context,
    get_intraday_context,
)
from tradingagents.agents.utils.structured import (
    bind_structured,
    invoke_structured_or_freetext,
)


def create_research_manager(llm):
    structured_llm = bind_structured(llm, ResearchPlan, "Research Manager")

    def research_manager_node(state) -> dict:
        instrument_context = build_instrument_context(state["company_of_interest"])
        intraday_context = get_intraday_context()
        history = state["investment_debate_state"].get("history", "")

        investment_debate_state = state["investment_debate_state"]

        prompt = f"""You are the Research Manager and debate facilitator. The Bull and Bear researchers have just argued about this stock as a same-day intraday long. Your job is to extract the strongest signal from that debate and hand the Trader an actionable entry plan.

{instrument_context}

{intraday_context}

---

**Your role: ranker, not gatekeeper.**

The desk runs this analysis on N stocks each morning. After every name is rated, an allocator picks the top 3 by ``confidence × R:R`` and trades those — the desk **always trades the best 3**. So your output is not "should we trade today?" — it is "how strongly does *this* stock rate against today's other candidates?"

**Your decision (use exactly one):**
- **Buy** — The default. Today is tradeable as an intraday long. The Bull's intraday case has at least *some* merit — it doesn't have to be perfect; the allocator will rank weak Buys lower and they likely won't be traded.
- **Skip** — Reserved for outright unsafe: no parseable levels, halted, scheduled binary event today (earnings tonight, regulatory ruling, etc.). **NOT** for "the bull case is weak" — weak setups get Buy with low confidence; the allocator handles weak.

**Hand-off rules for the Trader:**
- For every Buy, your Strategic Actions section must give the Trader concrete starting numbers: proposed entry zone, SL anchor (e.g. "below today's VWAP" or "below opening-range low"), T1 anchor, position-size guidance (% of capital), and a skip-rule cutoff time. Even for low-conviction Buys, give numbers — let the Trader and PM calibrate.
- For Skip, briefly explain the unsafe condition (event risk, no parseable levels) so the Trader doesn't override you.

**How to evaluate the debate:**
1. Identify the strongest 1-2 arguments from each side that are *actually about today's price action*. Discard arguments about multi-quarter thesis, long-term valuation, or strategic narrative — those don't move price intraday.
2. Compare those arguments. The Bull's job is to argue today has any tradeable edge; the Bear's job is to argue this setup is *weaker than alternatives*, not to argue we should sit out.
3. Default to Buy unless the Bear surfaces an outright unsafe condition. The rationale field should make the rank-relative quality of this setup clear (strong / decent / weak), so the Trader and PM can calibrate confidence accordingly.

---

**Debate History (Bull vs Bear):**
{history}

Speak in plain prose, as if briefing the Trader at the morning meeting."""

        investment_plan = invoke_structured_or_freetext(
            structured_llm,
            llm,
            prompt,
            render_research_plan,
            "Research Manager",
        )

        new_investment_debate_state = {
            "judge_decision": investment_plan,
            "history": investment_debate_state.get("history", ""),
            "bear_history": investment_debate_state.get("bear_history", ""),
            "bull_history": investment_debate_state.get("bull_history", ""),
            "current_response": investment_plan,
            "count": investment_debate_state["count"],
        }

        return {
            "investment_debate_state": new_investment_debate_state,
            "investment_plan": investment_plan,
        }

    return research_manager_node
