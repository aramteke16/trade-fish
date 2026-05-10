from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
from tradingagents.agents.utils.agent_utils import (
    build_instrument_context,
    get_intraday_context,
    get_language_instruction,
    get_news,
)
from tradingagents.dataflows.config import get_config


def create_sentiment_retail_analyst(llm):
    def sentiment_retail_node(state):
        current_date = state["trade_date"]
        instrument_context = build_instrument_context(state["company_of_interest"])
        intraday_context = get_intraday_context()

        tools = [
            get_news,
        ]

        system_message = (
            f"{intraday_context}\n\n"
            "You are the Retail Sentiment Analyst on an intraday trading desk for the Indian "
            "stock market. Your job is to read what retail (small) investors are doing and "
            "feeling about this stock **right now**, and convert that into an intraday signal.\n\n"
            "**What to look at:**\n"
            "- Social media buzz on Twitter/X, Reddit (r/IndiaInvestments, r/stocks), Telegram "
            "channels, StockTwits.\n"
            "- Retail broker activity: spike in retail order flow, MoneyControl 'most discussed' "
            "lists, Smallcase popularity.\n"
            "- Google Trends: rising / falling search interest in the ticker name.\n"
            "- Forum tone: are retail investors FOMO-buying, capitulating, or indifferent?\n\n"
            "**Why this matters intraday:**\n"
            "- Sharp retail FOMO into a stock often marks the *late* stage of a move — the marginal "
            "buyer is exhausted, and the stock can roll over within hours. This is a SKIP flag.\n"
            "- Retail panic into a stock that institutional flow is buying can be a BUY flag — "
            "smart money fading retail fear is a classic intraday setup.\n"
            "- Retail indifference + improving technicals = clean Buy environment, no contrarian "
            "noise.\n\n"
            "**Output format:**\n"
            "1. **Retail tone right now**: Bullish / Neutral / Bearish, with 2-3 specific evidence "
            "points (quote a tweet, cite a forum thread, mention a search-trend spike).\n"
            "2. **Intraday read**: how does retail sentiment intersect with today's trade? E.g., "
            "'Late-stage retail FOMO — caution', 'Retail panic vs. institutional buying — "
            "contrarian Buy setup', 'Retail indifferent — sentiment-neutral, defer to technicals'.\n"
            "3. Append a Markdown table summarizing each signal source and its read.\n\n"
            "Keep it concise — 2-3 paragraphs of prose, then the table. Don't write essays; the "
            "desk needs an actionable signal, not a deep-dive."
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
            "sentiment_retail_report": report,
        }

    return sentiment_retail_node
