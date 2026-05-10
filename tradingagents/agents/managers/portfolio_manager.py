"""Portfolio Manager: synthesises the risk-analyst debate into the final intraday decision.

Uses LangChain's ``with_structured_output`` so the LLM produces a typed
``PortfolioDecision`` directly, in a single call.  The result is rendered
back to markdown for storage in ``final_trade_decision`` so memory log,
CLI display, and saved reports continue to consume the same shape they do
today.  When a provider does not expose structured output, the agent falls
back gracefully to free-text generation.
"""

from __future__ import annotations

from tradingagents.agents.schemas import PortfolioDecision, render_pm_decision
from tradingagents.agents.utils.agent_utils import (
    build_instrument_context,
    get_intraday_context,
    get_language_instruction,
)
from tradingagents.agents.utils.structured import (
    bind_structured,
    invoke_structured_or_freetext,
)


def create_portfolio_manager(llm):
    structured_llm = bind_structured(llm, PortfolioDecision, "Portfolio Manager")

    def portfolio_manager_node(state) -> dict:
        instrument_context = build_instrument_context(state["company_of_interest"])
        intraday_context = get_intraday_context()

        history = state["risk_debate_state"]["history"]
        risk_debate_state = state["risk_debate_state"]
        research_plan = state["investment_plan"]
        trader_plan = state["trader_investment_plan"]

        past_context = state.get("past_context", "")
        lessons_line = (
            f"**Lessons from prior decisions and outcomes (use these to avoid "
            f"past mistakes and double down on what worked):**\n{past_context}\n\n"
            if past_context
            else ""
        )

        prompt = f"""You are the Portfolio Manager, the final decision-maker on the trading desk. Your job is to synthesize the risk-analyst debate and **rate this stock's intraday quality** so the desk can rank it against today's other candidates.

{instrument_context}

{intraday_context}

---

**Your role: ranker, not gatekeeper.**

The desk runs this analysis on N stocks every morning. After every name is rated, an allocator picks the top 3 by ``confidence × reward-to-risk`` and distributes capital. The desk **always trades the best 3** — you are not deciding whether to trade, you are deciding **how strongly *this* stock ranks**.

**Your decision (use exactly one):**
- **Buy** — Today is tradeable as an intraday long. There are parseable levels (entry, SL, T1) and the setup will be ranked against the day's other names by your confidence score.
- **Skip** — Reserved for outright unsafe situations only:
  * No parseable price (halted, suspended mid-session)
  * Major scheduled event that creates binary risk (earnings tonight, regulatory ruling pending, ex-dividend with large amount today)
  * Levels cannot be set sensibly (entry would be far above current price, SL would breach the 1.5%-of-capital risk budget at any size)

  **Skip is NOT for "meh" setups.** A weak setup gets Buy with low confidence (3-4/10) — the allocator will rank it last and it likely won't be traded. That's how the system filters mediocrity. *You* should default to Buy and let confidence reflect quality.

**Confidence calibration (1-10):**
- **9-10**: Crystal-clear setup. Strong momentum, clean levels, multiple confluences (VWAP-aligned, indicators agree, sector tailwind, institutional flow supportive).
- **7-8**: Solid setup with one or two minor reservations. Most factors align.
- **5-6**: Decent — tradeable but not exceptional. The bear has real points but the bull case wins on balance.
- **3-4**: Weak — barely tradeable. Levels exist but conviction is low. Will likely rank last and not be traded.
- **1-2**: Almost unsafe but not quite Skip-worthy. Use sparingly.

Spread your scores honestly across this range. **If everything you analyze comes back at 5/10, the allocator's rank is meaningless.**

---

**Inputs you must integrate:**

**Research Manager's intraday entry plan:**
{research_plan}

**Trader's transaction proposal (concrete levels):**
{trader_plan}

{lessons_line}**Risk-analyst debate history (Aggressive / Conservative / Neutral):**
{history}

---

**How to decide:**
1. Pick the single argument from each risk perspective (Aggressive, Conservative, Neutral) that you find most credible. Reject the weak parts.
2. Sanity-check the Trader's levels: entry zone, SL, T1, T2, time-of-day cutoff. Override their numbers if needed — yours are final.
3. Set confidence honestly across the full 1-10 range. The desk only sees today; there is no "wait for tomorrow". Rate this stock relative to the kind of intraday setups you'd expect on an average day, not relative to a perfect setup.
4. Write a short executive summary covering: entry, SL, first target, suggested position size (the allocator will override), time-of-day cutoff, and the single most important risk being accepted.
5. Anchor the investment thesis in specific evidence from the analysts and the debate — reference indicator readings, news catalysts, or risk-team points by name. No generic optimism.

Be decisive. The default is Buy with an honest confidence. Skip is only for outright unsafe situations.{get_language_instruction()}"""

        final_trade_decision = invoke_structured_or_freetext(
            structured_llm,
            llm,
            prompt,
            render_pm_decision,
            "Portfolio Manager",
        )

        new_risk_debate_state = {
            "judge_decision": final_trade_decision,
            "history": risk_debate_state["history"],
            "aggressive_history": risk_debate_state["aggressive_history"],
            "conservative_history": risk_debate_state["conservative_history"],
            "neutral_history": risk_debate_state["neutral_history"],
            "latest_speaker": "Judge",
            "current_aggressive_response": risk_debate_state["current_aggressive_response"],
            "current_conservative_response": risk_debate_state["current_conservative_response"],
            "current_neutral_response": risk_debate_state["current_neutral_response"],
            "count": risk_debate_state["count"],
        }

        return {
            "risk_debate_state": new_risk_debate_state,
            "final_trade_decision": final_trade_decision,
        }

    return portfolio_manager_node
