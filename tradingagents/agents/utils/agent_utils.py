from langchain_core.messages import HumanMessage, RemoveMessage

# Import tools from separate utility files
from tradingagents.agents.utils.core_stock_tools import (
    get_stock_data
)
from tradingagents.agents.utils.technical_indicators_tools import (
    get_indicators
)
from tradingagents.agents.utils.fundamental_data_tools import (
    get_fundamentals,
    get_balance_sheet,
    get_cashflow,
    get_income_statement
)
from tradingagents.agents.utils.news_data_tools import (
    get_news,
    get_insider_transactions,
    get_global_news
)


def get_language_instruction() -> str:
    """Return a prompt instruction for the configured output language.

    Returns empty string when English (default), so no extra tokens are used.
    Only applied to user-facing agents (analysts, portfolio manager).
    Internal debate agents stay in English for reasoning quality.
    """
    from tradingagents.dataflows.config import get_config
    lang = get_config().get("output_language", "English")
    if lang.strip().lower() == "english":
        return ""
    return f" Write your entire response in {lang}."


def build_instrument_context(ticker: str) -> str:
    """Describe the exact instrument so agents preserve exchange-qualified tickers."""
    return (
        f"The instrument to analyze is `{ticker}`. "
        "Use this exact ticker in every tool call, report, and recommendation, "
        "preserving any exchange suffix (e.g. `.TO`, `.L`, `.HK`, `.T`)."
    )


def get_intraday_context() -> str:
    """Canonical intraday-trading context block, injected into every agent prompt.

    Distilled from standard NSE-India intraday practice (Zerodha Varsity, MIS
    auto-square-off rules, ATR-based stop-loss conventions, VWAP/RSI/opening-
    range guidance). Establishes the operating regime so every agent reasons
    on the same timeframe and risk constraints rather than drifting into
    multi-quarter investment thinking.
    """
    return (
        "**Trading Regime (Same-Day Intraday — NSE India, long-only):**\n"
        "- Long-only, same-day square-off. Output for each stock is BUY (this "
        "is tradeable today, here's the setup quality) or SKIP (outright "
        "unsafe — halted, scheduled binary event, no parseable levels).\n"
        "- The desk runs analysis on N stocks each morning, ranks them by "
        "(confidence × R:R), and **always trades the top 3**. There is no "
        "'wait for tomorrow' — this framework only sees today. Rate honestly "
        "so the ranker picks the best setups; weak setups get a Buy with low "
        "confidence (3-4/10) and the allocator de-prioritizes them.\n"
        "- Entry window: 09:15-11:30 IST. The first 15 minutes (09:15-09:30) "
        "are usually noise — wait for opening range to form. After 11:30, no "
        "fresh entries: not enough time left to hit T1 before square-off.\n"
        "- Hard exit: every position is force-closed by 15:15 IST regardless "
        "of P&L. T1 and T2 must be realistic to hit before then in current "
        "volatility — use ATR as the speed-of-price measure.\n"
        "- Per-trade risk budget: ≤1.5% of capital. Stop-loss must be tight "
        "enough that (entry − SL) × position size ≤ 1.5% of capital. A wider "
        "SL forces a smaller position, which usually means worse R:R.\n"
        "- Stop-loss placement: 1.0-1.5x ATR(14) below entry, OR below the "
        "nearest intraday structural support (VWAP, prior swing low, opening "
        "range low) — whichever is tighter and still gives the trade room to "
        "breathe.\n"
        "- Reward-to-risk: aim for ≥1.5:1 to T1 and ≥2.5:1 to T2. Lower R:R "
        "means lower confidence and lower rank — not necessarily Skip.\n"
        "- Holding period: minutes to a few hours. Catalysts that play out over "
        "weeks or quarters (annual results, multi-year guidance, long-term "
        "competitive moats) are noise for this timeframe — ignore them. What "
        "matters is what moves price today: pre-market gaps, intraday news, "
        "block deals, sector rotation, index moves, FII/DII flow.\n"
        "- Bias filter: be aligned with VWAP (price above VWAP = long-friendly), "
        "RSI(14) on hourly between 50-70 (above-trend, not yet overbought), and "
        "the broader index (Nifty50) intraday direction. Counter-trend "
        "intraday Buys are low-confidence (and thus low-rank), not auto-Skip."
    )

def create_msg_delete():
    def delete_messages(state):
        """Clear messages and add placeholder for Anthropic compatibility"""
        messages = state["messages"]

        # Remove all messages
        removal_operations = [RemoveMessage(id=m.id) for m in messages]

        # Add a minimal placeholder message
        placeholder = HumanMessage(content="Continue")

        return {"messages": removal_operations + [placeholder]}

    return delete_messages


        
