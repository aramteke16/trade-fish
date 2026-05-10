from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
from tradingagents.agents.utils.agent_utils import (
    build_instrument_context,
    get_intraday_context,
    get_language_instruction,
    get_news,
)
from tradingagents.dataflows.config import get_config


def create_social_media_analyst(llm):
    def social_media_analyst_node(state):
        current_date = state["trade_date"]
        instrument_context = build_instrument_context(state["company_of_interest"])
        intraday_context = get_intraday_context()

        tools = [
            get_news,
        ]

        system_message = (
            f"{intraday_context}\n\n"
            "You are a social-media and company-news analyst on an intraday trading desk. Your "
            "job is to surface what's being said about this company across social media, news "
            "wires, and forums in the last 1-3 days, and convert that into an intraday signal.\n\n"
            "**What matters:**\n"
            "- Viral posts, breaking-news tweets, sudden surges in mention volume — these often "
            "precede intraday moves by minutes.\n"
            "- Sentiment shifts: a stock that was trending positive yesterday but went sour "
            "overnight on a single news item.\n"
            "- Influencer / analyst commentary going retail-viral (a Bloomberg article shared 50K "
            "times in 4 hours hits the tape).\n\n"
            "**What to deprioritize:** evergreen forum posts, generic positive/negative chatter "
            "without a specific date trigger, opinions older than 3 days.\n\n"
            "Use get_news(query, start_date, end_date) for company-specific news and discussions. "
            "Output a concise report (Bullish / Neutral / Bearish + 2-3 evidence points), append "
            "a Markdown table summarizing each signal, and end with an **Intraday read** one-liner."
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
            "sentiment_report": report,
        }

    return social_media_analyst_node
