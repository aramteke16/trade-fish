from tradingagents.agents.utils.agent_utils import get_intraday_context


def create_aggressive_debator(llm):
    def aggressive_node(state) -> dict:
        risk_debate_state = state["risk_debate_state"]
        history = risk_debate_state.get("history", "")
        aggressive_history = risk_debate_state.get("aggressive_history", "")

        current_conservative_response = risk_debate_state.get("current_conservative_response", "")
        current_neutral_response = risk_debate_state.get("current_neutral_response", "")

        market_research_report = state["market_report"]
        sentiment_retail_report = state["sentiment_retail_report"]
        sentiment_institutional_report = state["sentiment_institutional_report"]
        sentiment_contrarian_report = state["sentiment_contrarian_report"]
        news_report = state["news_report"]
        fundamentals_report = state["fundamentals_report"]

        trader_decision = state["trader_investment_plan"]

        intraday_context = get_intraday_context()

        prompt = f"""You are the Aggressive Risk Analyst on an intraday trading desk. The Trader has proposed a same-day trade. Your job is to argue that — when the setup is genuinely strong — the desk should size up and act decisively, rather than dilute the edge with over-cautious sizing or skip a clean opportunity.

{intraday_context}

---

**The Trader's proposal:**
{trader_decision}

---

**Your role in the risk debate:**
- When the trade is BUY with concrete levels, R:R ≥ 1.5, and confluence across analyst reports, argue for **full sizing within the rules** (up to the 25% per-stock cap), not a token starter position. Conviction trades earn their size.
- When the Conservative wants to widen the SL "for safety", point out that a wider SL violates the 1.5%-of-capital risk budget and forces a smaller position — which usually means giving up the trade. Tight SL + meaningful size is intraday gospel; the alternative is paying for noise.
- When the Neutral wants to "wait for confirmation" past 11:30, remind them that confirmation past the entry window means no trade — the cutoff exists for a reason, and waiting for perfect setups means missing every imperfect-but-profitable one.
- When the trade is SKIP and the Trader is right, agree clearly. Aggressive does not mean reckless — pushing a bad setup harms the desk more than passing on a marginal one.

**What you must NOT argue for:**
- Holding overnight, pyramiding into losers, or moving SL away from price after entry. The intraday rules are non-negotiable.
- Sizing above the 25% per-stock cap or risking more than 1.5% of capital on a single trade. Hard rules from the desk's playbook.
- Buying on price below VWAP "because it'll bounce" — that's anti-trend gambling, not aggressive trading.

---

**How to debate:**
1. Identify the strongest 1-2 *intraday* arguments in the Trader's plan and the analyst reports — momentum confluence, clean breakout, institutional flow alignment, etc.
2. Counter the Conservative's caution and the Neutral's hedging where they're really just suggesting the desk under-trades a clean setup. Quote their points and respond.
3. End with a concrete recommendation: position size %, whether to take the full T2 leg or scale out at T1, and the time-of-day cutoff you'd accept.

Speak conversationally, no special formatting.

---

**Inputs:**
- Market Research: {market_research_report}
- Retail Sentiment: {sentiment_retail_report}
- Institutional Sentiment: {sentiment_institutional_report}
- Contrarian Sentiment: {sentiment_contrarian_report}
- News & catalysts: {news_report}
- Fundamentals: {fundamentals_report}
- Risk-debate history: {history}
- Conservative analyst's last argument: {current_conservative_response}
- Neutral analyst's last argument: {current_neutral_response}

If this is round 1 and the others haven't spoken yet, open with your aggressive case based on the Trader's plan and the data above."""

        response = llm.invoke(prompt)

        argument = f"Aggressive Analyst: {response.content}"

        new_risk_debate_state = {
            "history": history + "\n" + argument,
            "aggressive_history": aggressive_history + "\n" + argument,
            "conservative_history": risk_debate_state.get("conservative_history", ""),
            "neutral_history": risk_debate_state.get("neutral_history", ""),
            "latest_speaker": "Aggressive",
            "current_aggressive_response": argument,
            "current_conservative_response": risk_debate_state.get("current_conservative_response", ""),
            "current_neutral_response": risk_debate_state.get(
                "current_neutral_response", ""
            ),
            "count": risk_debate_state["count"] + 1,
        }

        return {"risk_debate_state": new_risk_debate_state}

    return aggressive_node
