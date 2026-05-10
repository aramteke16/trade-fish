from tradingagents.graph.trading_graph import TradingAgentsGraph
from tradingagents.default_config import DEFAULT_CONFIG

from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

# Create a custom config
config = DEFAULT_CONFIG.copy()

# LLM Provider: "anthropic" with CLI mode (uses Claude Code + Bedrock gateway)
# To switch to direct API, change claude_mode to "api" and set ANTHROPIC_API_KEY
config["llm_provider"] = "anthropic"
config["claude_mode"] = "cli"  # "cli" = Claude Code CLI via Bedrock, "api" = direct Anthropic API
config["deep_think_llm"] = "claude-sonnet-4-5-20250514"
config["quick_think_llm"] = "claude-sonnet-4-5-20250514"
config["max_debate_rounds"] = 1

# Configure data vendors (default uses yfinance, no extra API keys needed)
config["data_vendors"] = {
    "core_stock_apis": "yfinance",
    "technical_indicators": "yfinance",
    "fundamental_data": "yfinance",
    "news_data": "yfinance",
}

# Initialize with custom config
ta = TradingAgentsGraph(debug=True, config=config)

# Test with an Indian mid-cap stock
_, decision = ta.propagate("TATAELXSI.NS", "2026-05-07")
print(decision)

# Memorize mistakes and reflect
# ta.reflect_and_remember(1000)
