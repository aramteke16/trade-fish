from tradingagents.agents.utils.agent_utils import get_intraday_context


def create_bear_researcher(llm):
    def bear_node(state) -> dict:
        investment_debate_state = state["investment_debate_state"]
        history = investment_debate_state.get("history", "")
        bear_history = investment_debate_state.get("bear_history", "")

        current_response = investment_debate_state.get("current_response", "")
        market_research_report = state["market_report"]
        sentiment_retail_report = state["sentiment_retail_report"]
        sentiment_institutional_report = state["sentiment_institutional_report"]
        sentiment_contrarian_report = state["sentiment_contrarian_report"]
        news_report = state["news_report"]
        fundamentals_report = state["fundamentals_report"]

        intraday_context = get_intraday_context()

        prompt = f"""You are the Bear Researcher on an intraday trading desk. Your job is to argue why this stock is a **weaker** intraday candidate than alternatives, so the desk's ranker can sort it correctly. The desk runs analysis on N stocks each day and trades the top 3 — your goal is **to push this stock down the rank**, not to advocate for sitting out.

{intraday_context}

---

**Your role: rank-down, don't gate.**

You are NOT arguing the desk should sit out the day. The desk always trades the top 3 of N. You are arguing this *specific* stock is weaker than its peers and should rank lower in today's roster.

**What counts as a strong rank-down argument:**
- Setup is unclean: price below VWAP, RSI extreme (above 75 or below 30), MACD diverging negatively, choppy intraday range with no clear direction.
- Stop-loss would have to be wider than 1.5x ATR to give the trade room — forces a smaller position with worse R:R than alternatives.
- R:R to T1 < 1.5 — the win rate required to be profitable is unrealistic given intraday costs (brokerage, STT, slippage).
- Crowded trade / retail FOMO peaked: marginal buyer exhausted; the move probably already happened.
- Index / sector against: Nifty50 / sector index intraday trend is negative, so a long here is fighting the tape.
- Liquidity inadequate: thin order book means slippage on entry/exit will eat the small intraday targets.

**When you SHOULD recommend an outright Skip (rare):**
- Major scheduled event today that creates binary risk: earnings tonight, RBI policy decision, regulatory ruling pending, F&O expiry close on a stock with weird OI build-up.
- Halted, suspended, or no parseable price data.
- Stop-loss cannot be set within sane intraday bounds at all (gappy stock with wild ATR).

**What you should NOT base your argument on:**
- Long-term valuation worries, multi-year competitive risks, or macro themes that don't move price today. Those are reasons to skip a swing trade, not necessarily an intraday one.
- Generic "high P/E" or "insider sales" arguments — these don't cause intraday price moves on their own.

---

**Your task:**
1. Critique the Bull's case point-by-point. Where their evidence is real but *intraday* execution is unclean (wide SL, bad R:R, against VWAP), say so. Where their argument leans on long-horizon thesis, call that out — "that's an investment case, not a same-day setup."
2. End with a strength-rated synthesis: **"Bear verdict: DOWNGRADE TO LOW CONFIDENCE [setup is weak, should rank last]"** / **"Bear verdict: MODERATE CONFIDENCE [tradeable but not best-of-roster]"** / **"Bear verdict: SKIP [outright unsafe — earnings/halt/no levels]"**. Reserve SKIP for the third bucket only.

Speak conversationally, like an analyst defending a rank-down call in front of the desk. Argue against the Bull's strength, not against the existence of any trade.

---

**Inputs:**
- Market Research (technical indicators, price action): {market_research_report}
- Retail Sentiment: {sentiment_retail_report}
- Institutional Sentiment (FII/DII, block deals): {sentiment_institutional_report}
- Contrarian Sentiment (crowded trade check): {sentiment_contrarian_report}
- News & macro catalysts: {news_report}
- Fundamentals (rarely matters intraday — only flag if there's a same-day landmine like earnings tonight): {fundamentals_report}
- Debate history so far: {history}
- Bull's last argument: {current_response}

If this is the first round and there is no Bull argument yet, open with your strongest reasons to skip today based on the data above."""

        response = llm.invoke(prompt)

        argument = f"Bear Analyst: {response.content}"

        new_investment_debate_state = {
            "history": history + "\n" + argument,
            "bear_history": bear_history + "\n" + argument,
            "bull_history": investment_debate_state.get("bull_history", ""),
            "current_response": argument,
            "count": investment_debate_state["count"] + 1,
        }

        return {"investment_debate_state": new_investment_debate_state}

    return bear_node
