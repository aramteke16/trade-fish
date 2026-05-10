from tradingagents.agents.utils.agent_utils import get_intraday_context


def create_conservative_debator(llm):
    def conservative_node(state) -> dict:
        risk_debate_state = state["risk_debate_state"]
        history = risk_debate_state.get("history", "")
        conservative_history = risk_debate_state.get("conservative_history", "")

        current_aggressive_response = risk_debate_state.get("current_aggressive_response", "")
        current_neutral_response = risk_debate_state.get("current_neutral_response", "")

        market_research_report = state["market_report"]
        sentiment_retail_report = state["sentiment_retail_report"]
        sentiment_institutional_report = state["sentiment_institutional_report"]
        sentiment_contrarian_report = state["sentiment_contrarian_report"]
        news_report = state["news_report"]
        fundamentals_report = state["fundamentals_report"]

        trader_decision = state["trader_investment_plan"]

        intraday_context = get_intraday_context()

        prompt = f"""You are the Conservative Risk Analyst on an intraday trading desk. Your job is to protect the desk's capital. Your default is SKIP — bad intraday trades destroy edge fast (taxes, slippage, brokerage), so the bar to actually deploy capital must be high.

{intraday_context}

---

**The Trader's proposal:**
{trader_decision}

---

**Your role in the risk debate:**
- Stress-test the Trader's plan and the Aggressive analyst's case. For every argument, ask: *what has to be true for us to lose money on this?* If the answer is "the move just doesn't happen by 15:15", that's a real risk — intraday trades are time-bounded.
- Push for SKIP whenever any of these is true: R:R to T1 < 1.5, SL would be wider than 1.5x ATR, confidence < 6/10, price below VWAP, RSI extreme, scheduled event today, broader index against the trade, or analyst reports diverge meaningfully.
- When the trade does survive your scrutiny and is a BUY, push for **smaller size, tighter SL, and an earlier skip-rule cutoff** (e.g., skip if not filled by 11:00 instead of 11:30) rather than refusing the trade outright.
- Always demand a specific time-of-day exit if T1 isn't hit — "exit by 14:30 if T1 not reached" is non-negotiable for intraday positions.

**Counter the Aggressive analyst's case:**
- When they argue for full sizing, point out concrete risk-budget math: position size × (entry − SL) must be ≤ 1.5% of capital. Show the math.
- When they dismiss the Neutral's caution, defend the value of waiting for cleaner confirmation — most money lost intraday is on premature entries.
- When they invoke "momentum" or "conviction", ask whether that conviction translates into a concrete *price* level, or whether it's just narrative.

**What you must NOT argue for:**
- Generic "the market is risky" doom that doesn't reference today's actual setup.
- Holding losing positions hoping they'll come back. Cut at SL, full stop.
- Skipping every trade because "you can lose money" — your job is to protect capital from *unfavorable* trades, not from all trades.

---

**How to debate:**
1. Identify the 1-2 weakest links in the Trader's plan and the Aggressive case. Be specific — name the missing R:R math, the wide SL, the unresolved analyst contradiction.
2. Counter the Aggressive analyst's last point directly. Quote them, then respond with intraday-specific risk math.
3. End with a concrete adjustment: "reduce size to X%, tighten SL to Y, add cutoff at HH:MM, OR skip entirely if Z."

Speak conversationally, no formatting.

---

**Inputs:**
- Market Research: {market_research_report}
- Retail Sentiment: {sentiment_retail_report}
- Institutional Sentiment: {sentiment_institutional_report}
- Contrarian Sentiment: {sentiment_contrarian_report}
- News & catalysts: {news_report}
- Fundamentals: {fundamentals_report}
- Risk-debate history: {history}
- Aggressive analyst's last argument: {current_aggressive_response}
- Neutral analyst's last argument: {current_neutral_response}

If this is round 1 and the others haven't spoken yet, open with your conservative case based on the Trader's plan and the data above."""

        response = llm.invoke(prompt)

        argument = f"Conservative Analyst: {response.content}"

        new_risk_debate_state = {
            "history": history + "\n" + argument,
            "aggressive_history": risk_debate_state.get("aggressive_history", ""),
            "conservative_history": conservative_history + "\n" + argument,
            "neutral_history": risk_debate_state.get("neutral_history", ""),
            "latest_speaker": "Conservative",
            "current_aggressive_response": risk_debate_state.get(
                "current_aggressive_response", ""
            ),
            "current_conservative_response": argument,
            "current_neutral_response": risk_debate_state.get(
                "current_neutral_response", ""
            ),
            "count": risk_debate_state["count"] + 1,
        }

        return {"risk_debate_state": new_risk_debate_state}

    return conservative_node
