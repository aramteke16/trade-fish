from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
from tradingagents.agents.utils.agent_utils import (
    build_instrument_context,
    get_intraday_context,
    get_language_instruction,
    get_news,
)
from tradingagents.dataflows.config import get_config


def create_sentiment_institutional_analyst(llm):
    def sentiment_institutional_node(state):
        current_date = state["trade_date"]
        instrument_context = build_instrument_context(state["company_of_interest"])
        intraday_context = get_intraday_context()

        tools = [
            get_news,
        ]

        system_message = (
            f"{intraday_context}\n\n"
            "You are the Institutional Sentiment Analyst on an intraday trading desk for the "
            "Indian stock market. Your job is to track what 'smart money' is doing — FIIs, DIIs, "
            "mutual funds, insurers, pension funds, prop desks, block-deal counterparties — and "
            "translate that into an intraday signal.\n\n"
            "**What to look at:**\n"
            "- **FII / DII net flows** (last 1-5 days): are foreigners net buyers or sellers in the "
            "broader market? Heavy FII selling days hurt every long; FII buying lifts everything.\n"
            "- **Block deals & bulk deals** in this stock or close peers in the last 30 days.\n"
            "- **Mutual fund holding changes**: AUM rotation into/out of this stock or sector.\n"
            "- **Promoter activity**: insider buying = strong bullish; insider selling = caution "
            "(but contextual — could be liquidity event, not a sell signal).\n"
            "- **Institutional research notes**: rating changes, target-price revisions from "
            "brokers in the last 5-10 days.\n\n"
            "**Why this matters intraday:**\n"
            "- Institutions place large orders that take *days* to execute. If FIIs/DIIs are "
            "accumulating a stock, intraday dips are often bought back by the same flow — Buy on dip.\n"
            "- If institutions are distributing (block sells, MF redemptions), every intraday "
            "rally tends to fade by close — high SKIP probability.\n"
            "- A fresh bulk-deal print today often triggers algo-driven buying for the next 1-3 "
            "hours — useful intraday momentum signal.\n\n"
            "**Output format:**\n"
            "1. **Institutional flow direction**: Accumulating / Distributing / Neutral, with 2-3 "
            "specific data points (FII net buy ₹X cr today, block deal of Y shares 2 days ago, "
            "MF holding +1.5% last quarter).\n"
            "2. **Intraday read**: how does institutional flow intersect with today's trade? E.g., "
            "'FII heavy buy + block-deal print today — bullish intraday tailwind', 'DIIs unloading "
            "into rallies — fade strength', 'No institutional signal — defer to technicals'.\n"
            "3. Append a Markdown table summarizing each signal source.\n\n"
            "Keep it concise. Specific numbers > vague qualitative takes."
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
            "sentiment_institutional_report": report,
        }

    return sentiment_institutional_node
