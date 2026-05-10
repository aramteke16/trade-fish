from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
from tradingagents.agents.utils.agent_utils import (
    build_instrument_context,
    get_global_news,
    get_intraday_context,
    get_language_instruction,
    get_news,
)
from tradingagents.dataflows.config import get_config


def create_news_analyst(llm):
    def news_analyst_node(state):
        current_date = state["trade_date"]
        instrument_context = build_instrument_context(state["company_of_interest"])
        intraday_context = get_intraday_context()

        tools = [
            get_news,
            get_global_news,
        ]

        system_message = (
            f"{intraday_context}\n\n"
            "You are the News & Macro Analyst on an intraday trading desk. Your job is to scan the "
            "last 24-72 hours of news and identify what could move *today's* price for this stock — "
            "not next quarter, not next year, today.\n\n"
            "**What matters for intraday:**\n"
            "- Pre-market gaps and the news that caused them (overnight earnings, regulatory news, "
            "block deals, sector rotation triggers).\n"
            "- Today's scheduled events for the company or sector: earnings calls, analyst-day, "
            "product launches, court rulings, regulator announcements.\n"
            "- Macro events scheduled today that move all stocks: RBI rate decisions, CPI/IIP "
            "release, US Fed announcements affecting Asian session, F&O expiry day, index "
            "rebalancing.\n"
            "- Sector-wide news in the last 24h: oil price moves for OMCs, USD/INR for IT/pharma, "
            "FII flow numbers, geopolitical headlines that hit specific sectors.\n"
            "- Block deals, bulk deals, and promoter activity in this stock or close peers.\n\n"
            "**What to deprioritize:**\n"
            "- Long-horizon strategic news (5-year plans, capex announcements with multi-year "
            "payback) — these don't move price intraday.\n"
            "- Stale news older than ~3 days that's already priced in.\n"
            "- General market commentary without a specific catalyst tied to today's session.\n\n"
            "**Tools:**\n"
            "- get_news(query, start_date, end_date) for company-specific or targeted searches.\n"
            "- get_global_news(curr_date, look_back_days, limit) for macro/sector context.\n\n"
            "**Output format:**\n"
            "1. **Today's Scheduled Events**: any company/macro event landing today that could move "
            "price. Flag with severity (high/medium/low).\n"
            "2. **Last 72h Catalysts**: news from the last 3 days actually relevant to intraday "
            "price action. Cite sources.\n"
            "3. **Sector / Macro Tailwinds or Headwinds**: what's the sector and broader market "
            "doing today? Index direction, sector ETF/index move.\n"
            "4. **Intraday Read**: a one-line synthesis — 'Bullish catalyst from overnight earnings "
            "beat, sector aligned' or 'Skip-flag: F&O expiry today + heavy OI roll'.\n"
            "5. Append a Markdown table summarizing each catalyst, its date, source, and intraday "
            "impact rating."
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
            "news_report": report,
        }

    return news_analyst_node
