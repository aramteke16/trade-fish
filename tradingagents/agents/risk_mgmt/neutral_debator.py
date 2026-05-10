from tradingagents.agents.utils.agent_utils import get_intraday_context


def create_neutral_debator(llm):
    def neutral_node(state) -> dict:
        risk_debate_state = state["risk_debate_state"]
        history = risk_debate_state.get("history", "")
        neutral_history = risk_debate_state.get("neutral_history", "")

        current_aggressive_response = risk_debate_state.get("current_aggressive_response", "")
        current_conservative_response = risk_debate_state.get("current_conservative_response", "")

        market_research_report = state["market_report"]
        sentiment_retail_report = state["sentiment_retail_report"]
        sentiment_institutional_report = state["sentiment_institutional_report"]
        sentiment_contrarian_report = state["sentiment_contrarian_report"]
        news_report = state["news_report"]
        fundamentals_report = state["fundamentals_report"]

        trader_decision = state["trader_investment_plan"]

        intraday_context = get_intraday_context()

        prompt = f"""You are the Neutral Risk Analyst on an intraday trading desk. Your job is to find the right *size and structure* for the trade — neither over-aggressive nor over-cautious. You take this trade if and only if the math works, and you size it where the desk can absorb a loss without a daily-loss-limit hit.

{intraday_context}

---

**The Trader's proposal:**
{trader_decision}

---

**Your role in the risk debate:**
- Apply the actual position-sizing formula and put numbers on the page. Position size = (1.5% of capital) / (entry − SL). If that math gives a tiny size, the trade has bad R:R or wide SL — push to either tighten SL, lower confidence, or skip.
- Critique both extremes: when Aggressive wants to size up at the cap, ask whether confidence and analyst confluence justify it. When Conservative wants to skip, ask whether the rule that fails is a real intraday rule or general market anxiety.
- Recommend a specific position-size %, scaling structure (50% off at T1, 50% at T2, or all-or-nothing), and skip-rule cutoff time. These are the levers you control.
- Where the Aggressive and Conservative disagree about a fact (e.g., "RSI is fine" vs "RSI is extreme"), arbitrate using the data in the analyst reports. Don't split the difference for the sake of it — pick the side the data supports.

**What balanced means in intraday:**
- Default position size for a confidence-7 trade with R:R 2.0 to T1: 15-20% of capital, not the 25% cap.
- Default position size for a confidence-6 trade with R:R 1.5 to T1: 10-15%, with strict skip-rule and tight SL.
- Below confidence-6 or below R:R 1.5: SKIP, regardless of how clean other parts look.
- Always scale out: 50% at T1, move SL to break-even, ride the rest to T2 or trail SL.

**What you must NOT do:**
- Recommend a position size without showing the (entry − SL) and capital math. Vague guidance like "moderate sizing" is useless.
- Argue for a Buy you don't believe in just to be "balanced" between the other two analysts. If both Aggressive and Conservative are wrong, say so and propose what's actually right.

---

**How to debate:**
1. Quote one specific point from the Aggressive analyst and one from the Conservative analyst that you'll either confirm or push back on.
2. Show the position-sizing math: capital × 1.5% / (entry − SL) = X shares. Convert to position-size %.
3. End with a concrete recommendation: "size at X%, scale 50/50 at T1/T2, skip-rule at HH:MM, BUY/SKIP" — pick one path.

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
- Conservative analyst's last argument: {current_conservative_response}

If this is round 1 and the others haven't spoken yet, open with your balanced case and the position-sizing math based on the Trader's plan."""

        response = llm.invoke(prompt)

        argument = f"Neutral Analyst: {response.content}"

        new_risk_debate_state = {
            "history": history + "\n" + argument,
            "aggressive_history": risk_debate_state.get("aggressive_history", ""),
            "conservative_history": risk_debate_state.get("conservative_history", ""),
            "neutral_history": neutral_history + "\n" + argument,
            "latest_speaker": "Neutral",
            "current_aggressive_response": risk_debate_state.get(
                "current_aggressive_response", ""
            ),
            "current_conservative_response": risk_debate_state.get("current_conservative_response", ""),
            "current_neutral_response": argument,
            "count": risk_debate_state["count"] + 1,
        }

        return {"risk_debate_state": new_risk_debate_state}

    return neutral_node
