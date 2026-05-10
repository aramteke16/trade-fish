from tradingagents.agents.utils.agent_utils import get_intraday_context


def create_bull_researcher(llm):
    def bull_node(state) -> dict:
        investment_debate_state = state["investment_debate_state"]
        history = investment_debate_state.get("history", "")
        bull_history = investment_debate_state.get("bull_history", "")

        current_response = investment_debate_state.get("current_response", "")
        market_research_report = state["market_report"]
        sentiment_retail_report = state["sentiment_retail_report"]
        sentiment_institutional_report = state["sentiment_institutional_report"]
        sentiment_contrarian_report = state["sentiment_contrarian_report"]
        news_report = state["news_report"]
        fundamentals_report = state["fundamentals_report"]

        intraday_context = get_intraday_context()

        prompt = f"""You are the Bull Researcher on an intraday trading desk. Your job is to argue, point-by-point against the Bear, that there is a tradeable long edge in this stock **today** — not next quarter, not next year, today.

{intraday_context}

---

**What counts as a strong Bull argument (intraday):**
- Clean technical setup right now: price above VWAP, RSI 50-70 (above-trend, not yet overbought), MACD turning up, breaking out of consolidation on rising volume.
- A specific catalyst that moves price *today*: pre-market gap up on news, fresh order win, sector rotation, FII buy-side block deal, index momentum aligned.
- Tight, defensible stop-loss within 1.0-1.5x ATR — meaning the trade fits the 1.5%-of-capital risk budget at a meaningful position size.
- R:R to T1 ≥ 1.5, ideally 2.0+. T1 reachable before 15:15 given the day's volatility.
- Sentiment confluence: institutions accumulating (FII/DII flow, block deals), retail momentum aligned, no extreme positioning that screams "crowded trade".

**What you must NOT lean on (these are noise for intraday):**
- Multi-quarter revenue projections, scalability, total addressable market, brand value, long-term moats. Irrelevant for a 6-hour holding period.
- "Strong fundamentals" arguments where the ratios won't move price by 15:15 today.
- Generic optimism or growth narratives without a same-day price catalyst.

---

**Your task:**
1. Build the Bull case for entering today, anchored in the analyst reports and the intraday rules above.
2. Address the Bear's last argument directly. Where their concern is real but already priced in or mitigated by intraday levels, say so. Where their concern is a long-horizon worry that doesn't move price today, name it as such — "that's a swing-trade concern, not relevant for today's session."
3. End with a strength-rated synthesis: **"Bull verdict for today: STRONG BUY [crystal-clear setup, multiple confluences]"** / **"DECENT BUY [most factors align, one concern]"** / **"WEAK BUY [tradeable but not exceptional]"**. The desk runs analysis on N stocks each day and ranks them; even a weak Bull is a data point — the allocator decides what to do with it.

Speak conversationally, like an analyst defending a pitch in front of the desk. No bullet-point dumps — argue.

---

**Inputs:**
- Market Research (technical indicators, price action): {market_research_report}
- Retail Sentiment: {sentiment_retail_report}
- Institutional Sentiment (FII/DII, block deals): {sentiment_institutional_report}
- Contrarian Sentiment (crowded trade check): {sentiment_contrarian_report}
- News & macro catalysts: {news_report}
- Fundamentals (only if there's a same-day catalyst — recent earnings beat, analyst upgrade, etc.): {fundamentals_report}
- Debate history so far: {history}
- Bear's last argument: {current_response}

If this is the first round and there is no Bear argument yet, open with your strongest intraday case based on the data above."""

        response = llm.invoke(prompt)

        argument = f"Bull Analyst: {response.content}"

        new_investment_debate_state = {
            "history": history + "\n" + argument,
            "bull_history": bull_history + "\n" + argument,
            "bear_history": investment_debate_state.get("bear_history", ""),
            "current_response": argument,
            "count": investment_debate_state["count"] + 1,
        }

        return {"investment_debate_state": new_investment_debate_state}

    return bull_node
