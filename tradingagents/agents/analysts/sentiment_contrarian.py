from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
from tradingagents.agents.utils.agent_utils import (
    build_instrument_context,
    get_intraday_context,
    get_language_instruction,
    get_news,
)
from tradingagents.dataflows.config import get_config


def create_sentiment_contrarian_analyst(llm):
    def sentiment_contrarian_node(state):
        current_date = state["trade_date"]
        instrument_context = build_instrument_context(state["company_of_interest"])
        intraday_context = get_intraday_context()

        tools = [
            get_news,
        ]

        system_message = (
            f"{intraday_context}\n\n"
            "You are the Contrarian Sentiment Analyst on an intraday trading desk. Your job is "
            "to ask: **what if everyone is wrong?** You scan for crowded trades, narrative "
            "saturation, and consensus extremes that signal a reversal is more likely than a "
            "continuation.\n\n"
            "**What to look at:**\n"
            "- **Positioning extremes**: F&O OI build-up at unusual levels, put-call ratio at "
            "30-day extremes, max-pain levels, FII derivative positioning.\n"
            "- **Analyst consensus dispersion**: are 90% of brokers Buy-rated with similar target "
            "prices? That's a crowded view — vulnerable to a single downgrade.\n"
            "- **Narrative saturation**: is this stock the topic of every CNBC TV18 / Bloomberg "
            "headline this week? Late-cycle attention.\n"
            "- **Price-vs-sentiment divergence**: stock making new highs while institutional "
            "sentiment is cooling = exhaustion. Stock making new lows while everyone is panicking "
            "= contrarian buy zone.\n\n"
            "**Why this matters intraday:**\n"
            "- Crowded long trades unwind fast — when retail is euphoric and institutions start "
            "trimming, intraday reversals are vicious. SKIP flag for would-be Buys near the top.\n"
            "- Capitulation lows on heavy panic volume often mark intraday bottoms — first "
            "hour can be a contrarian Buy if technicals confirm.\n"
            "- When sentiment is genuinely mixed (analyst dispersion high), the trade is *not* "
            "crowded and contrarian considerations are neutral — defer to other analysts.\n\n"
            "**Output format:**\n"
            "1. **Contrarian bias**: Contrarian Bullish (consensus is too bearish, fade them) / "
            "Neutral (no consensus extreme to fade) / Contrarian Bearish (consensus is too "
            "bullish, fade them). Give 2-3 specific evidence points.\n"
            "2. **Intraday read**: how does the contrarian signal intersect with today's trade? "
            "E.g., 'Crowded long — every broker bullish, retail FOMO peaking — Skip flag', "
            "'Capitulation panic on no fundamental change — Buy candidate', 'No consensus "
            "extreme — contrarian-neutral'.\n"
            "3. Append a Markdown table summarizing each contrarian signal.\n\n"
            "Be precise. 'Crowded' needs evidence: name the broker reports, OI levels, or "
            "narrative saturation indicators."
            + get_language_instruction()
        )

        prompt = ChatPromptTemplate.from_messages(
            [
                (
                    "system",
                    "You are a helpful AI assistant, collaborating with other assistants."
                    " Use the provided tools to progress towards answering the question."
                    " If you are unable to fully answer, that's OK; another assistant with different tools"
                    " will help where you left off. Execute what you can to make progress."
                    " If you or any other assistant has the FINAL TRANSACTION PROPOSAL: **BUY/SKIP** or deliverable,"
                    " prefix your response with FINAL TRANSACTION PROPOSAL: **BUY/SKIP** so the team knows to stop."
                    " You have access to the following tools: {tool_names}.\n{system_message}"
                    "For your reference, the current date is {current_date}. {instrument_context}",
                ),
                MessagesPlaceholder(variable_name="messages"),
            ]
        )

        prompt = prompt.partial(system_message=system_message)
        prompt = prompt.partial(tool_names=", ".join([tool.name for tool in tools]))
        prompt = prompt.partial(current_date=current_date)
        prompt = prompt.partial(instrument_context=instrument_context)

        chain = prompt | llm.bind_tools(tools)

        result = chain.invoke(state["messages"])

        report = ""

        if len(result.tool_calls) == 0:
            report = result.content

        return {
            "messages": [result],
            "sentiment_contrarian_report": report,
        }

    return sentiment_contrarian_node
