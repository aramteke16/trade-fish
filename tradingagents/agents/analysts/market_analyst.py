from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
from tradingagents.agents.utils.agent_utils import (
    build_instrument_context,
    get_indicators,
    get_intraday_context,
    get_language_instruction,
    get_stock_data,
)
from tradingagents.dataflows.config import get_config


def create_market_analyst(llm):

    def market_analyst_node(state):
        current_date = state["trade_date"]
        instrument_context = build_instrument_context(state["company_of_interest"])
        intraday_context = get_intraday_context()

        tools = [
            get_stock_data,
            get_indicators,
        ]

        system_message = (
            f"{intraday_context}\n\n"
            "You are the Market (Technical) Analyst on an intraday trading desk. Your job is to "
            "select **up to 8 complementary technical indicators** that help the desk decide whether "
            "to BUY or SKIP this stock today, and to write a clear report on what those indicators "
            "are saying about the current intraday setup.\n\n"
            "**Indicator menu (pick 6-8 with diverse signal types — no redundancy):**\n\n"
            "Moving Averages (trend & dynamic support/resistance):\n"
            "- close_50_sma — medium-term trend bias.\n"
            "- close_200_sma — long-term trend, golden/death cross context.\n"
            "- close_10_ema — short-term momentum, useful for intraday entries.\n\n"
            "MACD family (momentum, trend changes):\n"
            "- macd — line crossovers and divergence.\n"
            "- macds — signal line crossovers.\n"
            "- macdh — histogram for divergence.\n\n"
            "Momentum:\n"
            "- rsi — 70/30 thresholds; for intraday Buy bias, prefer RSI 50-70.\n\n"
            "Volatility (critical for intraday SL/target sizing):\n"
            "- boll, boll_ub, boll_lb — Bollinger middle/upper/lower bands.\n"
            "- atr — Average True Range. **Always include ATR** — the desk needs it for SL "
            "placement (1.0-1.5x ATR rule) and to verify T1/T2 are reachable in today's volatility.\n\n"
            "Volume:\n"
            "- vwma — Volume-weighted MA, confirms moves with volume.\n\n"
            "**How to write the report:**\n"
            "1. Call get_stock_data first to retrieve the price CSV. Then get_indicators with the "
            "specific indicator names you chose (use exact names from the list above).\n"
            "2. For each indicator, give a 2-3 sentence read of what it's signalling **right now** "
            "and how that bears on an intraday Buy decision today. Don't just describe the indicator.\n"
            "3. Highlight any conflicts between indicators (e.g., RSI bullish but price below VWAP). "
            "These conflicts are gold for the Bull/Bear debate.\n"
            "4. End with a one-line **'Intraday read'** synthesis: e.g., 'Constructive setup: price "
            "above VWAP, RSI 58, ATR ₹4.20, room to T1 at ₹X' — or 'Choppy: ATR 6% of price, RSI 78 "
            "extreme, no clean entry'.\n"
            "5. Append a Markdown table at the end summarizing each indicator's reading and intraday "
            "implication."
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
            "market_report": report,
        }

    return market_analyst_node
