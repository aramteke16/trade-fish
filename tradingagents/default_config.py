"""Static defaults + metadata for the DB-backed config service.

This module is the **seed source** for the ``app_config`` SQLite table. On
first boot (or when the table is empty), every key in ``DEFAULT_CONFIG`` is
inserted into the DB. From that point on, the runtime reads config from the
DB via ``tradingagents.web.config_service.get_full_config_with_env_overrides``,
which lets the user edit values via REST/UI without redeploying.

Storage path resolution (``_resolve_home``):
  1. ``TRADINGAGENTS_HOME`` env var — explicit override.
  2. ``/data`` if it exists and is writable — the cloud-volume convention
     (Render, Fly, ECS, GKE all mount persistent volumes there by default).
  3. ``~/.tradingagents`` — local-dev fallback.

Same priority chain applies to all derived paths (logs/, cache/, memory/,
reports/) and to the SQLite DB file itself in ``web/database.py``.
"""

import os


def _resolve_home() -> str:
    """Pick the base directory for all persistent state.

    See module docstring for the priority chain.
    """
    explicit = os.getenv("TRADINGAGENTS_HOME")
    if explicit:
        return explicit
    cloud_default = "/data"
    if os.path.isdir(cloud_default) and os.access(cloud_default, os.W_OK):
        return cloud_default
    return os.path.join(os.path.expanduser("~"), ".tradingagents")


_TRADINGAGENTS_HOME = _resolve_home()


DEFAULT_CONFIG = {
    "project_dir": os.path.abspath(os.path.join(os.path.dirname(__file__), ".")),
    "results_dir": os.getenv("TRADINGAGENTS_RESULTS_DIR", os.path.join(_TRADINGAGENTS_HOME, "logs")),
    "data_cache_dir": os.getenv("TRADINGAGENTS_CACHE_DIR", os.path.join(_TRADINGAGENTS_HOME, "cache")),
    "memory_log_path": os.getenv("TRADINGAGENTS_MEMORY_LOG_PATH", os.path.join(_TRADINGAGENTS_HOME, "memory", "trading_memory.md")),
    "reports_dir": os.getenv("TRADINGAGENTS_REPORTS_DIR", os.path.join(_TRADINGAGENTS_HOME, "reports")),
    # Cap on resolved memory log entries. 800 ≈ 12 months at 3 trades/day.
    # None disables rotation (unbounded).
    "memory_log_max_entries": 800,
    # LLM settings
    "llm_provider": "moonshot",
    "deep_think_llm": "kimi-k2.6",
    "quick_think_llm": "kimi-k2.5",
    "backend_url": None,
    # Provider-specific thinking configuration
    "google_thinking_level": None,
    "openai_reasoning_effort": None,
    "anthropic_effort": None,
    # Moonshot Kimi configuration
    "moonshot_api_key": None,
    "moonshot_base_url": "https://api.moonshot.ai/v1",
    "anthropic_api_key": None,
    "openai_api_key": None,
    "google_api_key": None,
    "xai_api_key": None,
    "deepseek_api_key": None,
    "dashscope_api_key": None,
    "zhipu_api_key": None,
    "openrouter_api_key": None,
    # Indian market settings
    "market_timezone": "Asia/Kolkata",
    "market_open": "09:15",
    "market_close": "15:30",
    "execution_window_start": "10:30",
    "execution_window_end": "15:15",
    "initial_capital": 20000,
    "capital_currency": "INR",
    "max_capital_per_stock_pct": 25,
    "max_loss_per_trade_pct": 1.5,
    "daily_loss_limit_pct": 3.0,
    "weekly_loss_limit_pct": 5.0,
    "hard_exit_time": "15:15",
    "skip_rule_time": "11:30",
    "min_liquidity_inr_crores": 5,
    # Cross-stock allocator (Phase 2.5)
    "top_k_positions": 3,
    "deploy_pct_top_k": 70.0,
    "target_daily_return_pct": 1.0,
    # Order placement
    "use_upper_band_only": True,
    "min_capital_to_trade": 5000,
    # Live monitor (Phase 4) — trailing-stop ladder
    "poll_interval_sec": 600,
    "breakeven_trigger_pct": 0.5,
    "trail_trigger_pct": 1.0,
    "trail_lock_pct": 0.3,
    # Mid-day news-event monitor
    "news_check_enabled": True,
    "news_check_lookback_min": 60,
    # End-of-day reflection sweep
    "eod_reflection_enabled": True,
    "eod_news_window_start": "09:00",
    "eod_news_window_end": "15:30",
    # Cron dispatcher — fixed 60s tick, time-based state transitions.
    "dispatcher_monitor_interval_sec": 600,     # throttle monitor.tick() to every N seconds
    "precheck_time": "08:10",                   # IST. Idle→precheck after this.
    "execution_time": "09:30",                  # IST. Waiting→monitor after this.
    # Checkpoint/resume
    "checkpoint_enabled": False,
    # Output language for analyst reports and final decision
    "output_language": "English",
    # Debate and discussion settings
    "max_debate_rounds": 2,
    "max_risk_discuss_rounds": 2,
    "max_recur_limit": 100,
    # Data vendor configuration (nested dicts; serialized as JSON in the DB row)
    "data_vendors": {
        "core_stock_apis": "yfinance",
        "technical_indicators": "yfinance",
        "fundamental_data": "yfinance",
        "news_data": "yfinance",
    },
    "tool_vendors": {},
    # Dry run E2E testing
    "dry_run_e2e": False,
    "dry_run_ticker": "RELIANCE.NS",
    "dry_run_plan": {
        "entry_zone_low": 1400.0, "entry_zone_high": 1410.0,
        "stop_loss": 1385.0, "target_1": 1440.0, "target_2": 1465.0,
        "confidence_score": 7, "position_size_pct": 15.0,
    },
    "dry_run_price_sequence": [
        1395.0, 1402.0, 1408.0, 1415.0,
        1422.0, 1435.0, 1441.0, 1450.0,
    ],
    # Telegram side-channel for live pipeline events. Disabled by default —
    # set the bot token + chat id and flip the toggle to start streaming
    # precheck / order / monitor / exit / EOD updates to Telegram.
    "telegram_notifications_enabled": False,
    "telegram_bot_token": "",
    "telegram_chat_id": "",
    # Scheduled boot/morning notifications.
    "telegram_startup_message_enabled": True,
    "telegram_morning_message_enabled": True,
    "telegram_morning_message_time": "08:00",
    # Internal — auto-managed by the dispatcher so the morning brief
    # doesn't double-fire after a reboot the same day. Do not edit by hand.
    "telegram_morning_message_last_date": "",
    # Report-file delivery to Telegram. Three independent toggles so you can
    # send per-ticker reports as they're generated, a single end-of-day zip
    # of everything, or both.
    "telegram_reports_enabled": True,
    "telegram_reports_per_ticker": True,
    "telegram_reports_eod_zip": True,
}


# ---------------------------------------------------------------------------
# CONFIG_METADATA — category + secrecy + tooltip text per key.
#
# Used by:
#   - seed_default_config_if_empty() to populate app_config rows
#   - GET /api/config to group keys for the UI and mask secrets
#   - PATCH /api/config to validate that the key is known
#
# Categories drive the UI grouping. "storage" rows are read with env-var
# precedence (env var > DB > seed default) so cloud overrides keep working.
# "secret" rows are masked as "***" in GET responses but pass through fully
# to the LLM clients via load_config().
# ---------------------------------------------------------------------------
CONFIG_METADATA = {
    # Storage paths
    "project_dir":     {"category": "storage", "is_secret": False, "description": "Package install directory (read-only, derived)."},
    "results_dir":     {"category": "storage", "is_secret": False, "description": "Per-run JSON state dumps from LangGraph."},
    "data_cache_dir":  {"category": "storage", "is_secret": False, "description": "yfinance / data fetch cache directory."},
    "memory_log_path": {"category": "storage", "is_secret": False, "description": "Markdown reflections log used as PM context."},
    "reports_dir":     {"category": "storage", "is_secret": False, "description": "Daily multi-agent markdown reports root: <reports_dir>/<DATE>/<TICKER>/."},
    "memory_log_max_entries": {"category": "storage", "is_secret": False, "description": "Cap on resolved memory entries; older ones pruned. None = unlimited."},

    # LLM provider settings
    "llm_provider":            {"category": "llm", "is_secret": False, "description": "Active LLM vendor.",
                                "options": ["moonshot", "anthropic", "openai", "google", "xai", "deepseek", "qwen", "glm", "openrouter", "ollama"]},
    "deep_think_llm":           {"category": "llm", "is_secret": False, "description": "Model for high-reasoning calls (PM, Research Manager).",
                                "provider_models": {
                                    "moonshot": ["kimi-k2.6", "kimi-k2.5"],
                                    "anthropic": ["claude-opus-4-7", "claude-sonnet-4-6"],
                                    "openai": ["gpt-4o", "o3-mini"],
                                    "google": ["gemini-2.5-pro"],
                                    "xai": ["grok-3"],
                                    "deepseek": ["deepseek-reasoner", "deepseek-chat"],
                                    "qwen": ["qwen3-235b-a22b"],
                                    "glm": ["glm-4-plus"],
                                    "openrouter": [],
                                    "ollama": [],
                                }},
    "quick_think_llm":          {"category": "llm", "is_secret": False, "description": "Model for fast / cheap calls (analysts, classifiers).",
                                "provider_models": {
                                    "moonshot": ["kimi-k2.5", "moonshot-v1-128k"],
                                    "anthropic": ["claude-haiku-4-5", "claude-sonnet-4-6"],
                                    "openai": ["gpt-4o-mini", "gpt-4o"],
                                    "google": ["gemini-2.0-flash", "gemini-2.5-pro"],
                                    "xai": ["grok-3-mini", "grok-3"],
                                    "deepseek": ["deepseek-chat"],
                                    "qwen": ["qwen-turbo", "qwen3-235b-a22b"],
                                    "glm": ["glm-4-flash", "glm-4-plus"],
                                    "openrouter": [],
                                    "ollama": [],
                                }},
    "backend_url":              {"category": "llm", "is_secret": False, "hidden": True, "description": "Provider base URL override; null = vendor default."},
    "google_thinking_level":    {"category": "llm", "is_secret": False, "hidden": True, "description": "Gemini thinking budget.",
                                "options": [None, "minimal", "high"]},
    "openai_reasoning_effort":  {"category": "llm", "is_secret": False, "hidden": True, "description": "OpenAI o-series reasoning effort.",
                                "options": [None, "low", "medium", "high"]},
    "anthropic_effort":         {"category": "llm", "is_secret": False, "hidden": True, "description": "Anthropic extended thinking effort.",
                                "options": [None, "low", "medium", "high"]},
    "output_language":          {"category": "llm", "is_secret": False, "description": "Output language for analyst reports.",
                                "options": ["English", "Hindi"]},
    "moonshot_api_key":         {"category": "llm", "is_secret": True,  "description": "Moonshot Kimi API key."},
    "moonshot_base_url":        {"category": "llm", "is_secret": False, "description": "Moonshot API base URL; rarely changed."},
    "anthropic_api_key":        {"category": "llm", "is_secret": True,  "description": "Anthropic API key (Claude models)."},
    "openai_api_key":           {"category": "llm", "is_secret": True,  "description": "OpenAI API key."},
    "google_api_key":           {"category": "llm", "is_secret": True,  "description": "Google AI API key (Gemini models)."},
    "xai_api_key":              {"category": "llm", "is_secret": True,  "description": "xAI API key (Grok models)."},
    "deepseek_api_key":         {"category": "llm", "is_secret": True,  "description": "DeepSeek API key."},
    "dashscope_api_key":        {"category": "llm", "is_secret": True,  "description": "Alibaba DashScope API key (Qwen models)."},
    "zhipu_api_key":            {"category": "llm", "is_secret": True,  "description": "Zhipu API key (GLM models)."},
    "openrouter_api_key":       {"category": "llm", "is_secret": True,  "description": "OpenRouter API key."},

    # Indian market & execution window
    "market_timezone":         {"category": "market", "is_secret": False, "description": "IANA timezone for market clock (default Asia/Kolkata)."},
    "market_open":             {"category": "market", "is_secret": False, "description": "NSE open in IST. Used by is_market_open().", "input_type": "time"},
    "market_close":            {"category": "market", "is_secret": False, "description": "NSE close in IST.", "input_type": "time"},
    "execution_window_start":  {"category": "market", "is_secret": False, "description": "Earliest entry time (IST). Pipeline waits until then.", "input_type": "time"},
    "execution_window_end":    {"category": "market", "is_secret": False, "description": "Latest entry time (IST). After this, no new entries.", "input_type": "time"},
    "hard_exit_time":          {"category": "market", "is_secret": False, "description": "Force-close all open positions at this time (IST).", "input_type": "time"},
    "skip_rule_time":          {"category": "market", "is_secret": False, "description": "Default skip-rule cutoff if PM doesn't set one.", "input_type": "time"},

    # Capital & risk
    "initial_capital":           {"category": "risk", "is_secret": False, "description": "Starting capital on first run. Subsequent runs use yesterday's EOD."},
    "capital_currency":          {"category": "risk", "is_secret": False, "description": "Display currency for capital + P&L."},
    "max_capital_per_stock_pct": {"category": "risk", "is_secret": False, "description": "Hard cap on capital deployed to any single stock (% of total)."},
    "max_loss_per_trade_pct":    {"category": "risk", "is_secret": False, "description": "Per-trade risk budget (% of capital). Drives position sizing."},
    "daily_loss_limit_pct":      {"category": "risk", "is_secret": False, "description": "Daily loss circuit breaker (% of initial). Trading pauses if hit."},
    "weekly_loss_limit_pct":     {"category": "risk", "is_secret": False, "description": "Weekly loss circuit breaker (% of initial)."},
    "min_liquidity_inr_crores":  {"category": "risk", "is_secret": False, "description": "Minimum daily-volume × price for screener (in ₹ crores)."},
    "target_daily_return_pct":   {"category": "risk", "is_secret": False, "description": "Informational target; allocator sizes for this."},

    # Allocator
    "top_k_positions":   {"category": "allocator", "is_secret": False, "description": "How many top-scored stocks to actually trade each day."},
    "deploy_pct_top_k":  {"category": "allocator", "is_secret": False, "description": "Total capital % deployed across top-K (half-Kelly cap)."},

    # Order placement
    "use_upper_band_only": {
        "category": "execution",
        "is_secret": False,
        "description": "When True, only the entry zone upper band is used at placement time: validation does not require entry_zone_low, and live-price adjustment anchors on the upper band (treated as a buy-limit at entry_zone_high).",
    },
    "min_capital_to_trade": {
        "category": "execution",
        "is_secret": False,
        "description": "Minimum free cash (INR) required to place any new order. If today's available capital falls below this, the placement phase is skipped entirely for the day.",
    },

    # Live-monitor knobs
    "poll_interval_sec":     {"category": "monitor", "is_secret": False, "description": "Seconds between price polls during execution window."},
    "breakeven_trigger_pct": {"category": "monitor", "is_secret": False, "description": "When unrealized P&L hits this %, raise SL to entry."},
    "trail_trigger_pct":     {"category": "monitor", "is_secret": False, "description": "When unrealized P&L hits this %, raise SL to entry+lock_pct."},
    "trail_lock_pct":        {"category": "monitor", "is_secret": False, "description": "Locked-in profit floor (% above entry) once trail trigger fires."},

    # News + EOD reflection
    "news_check_enabled":     {"category": "news_eod", "is_secret": False, "description": "Run mid-day news classifier on each poll."},
    "news_check_lookback_min": {"category": "news_eod", "is_secret": False, "description": "How far back to scan news (minutes) per poll."},
    "eod_reflection_enabled": {"category": "news_eod", "is_secret": False, "description": "Run post-market per-trade reflection sweep."},
    "eod_news_window_start":  {"category": "news_eod", "is_secret": False, "description": "Start of trade-day news window for EOD reflection (IST).", "input_type": "time"},
    "eod_news_window_end":    {"category": "news_eod", "is_secret": False, "description": "End of trade-day news window for EOD reflection (IST).", "input_type": "time"},

    # Debate / agent rounds
    "max_debate_rounds":       {"category": "debate", "is_secret": False, "description": "Bull/Bear debate rounds per stock."},
    "max_risk_discuss_rounds": {"category": "debate", "is_secret": False, "description": "Aggressive/Conservative/Neutral debate rounds per stock."},
    "max_recur_limit":         {"category": "debate", "is_secret": False, "description": "LangGraph recursion limit (safety bound)."},

    # Data vendors
    "data_vendors": {"category": "vendors", "is_secret": False, "description": "Default vendor per data category. JSON object."},
    "tool_vendors": {"category": "vendors", "is_secret": False, "description": "Tool-level vendor overrides. JSON object."},

    # System
    "checkpoint_enabled": {"category": "system", "is_secret": False, "description": "LangGraph checkpoint/resume after each node."},

    # Cron dispatcher (fixed 60s tick)
    "dispatcher_monitor_interval_sec": {"category": "dispatcher", "is_secret": False, "description": "Throttle monitor.tick() to run every N seconds (default 600 = 10 min)."},
    "precheck_time":                    {"category": "dispatcher", "is_secret": False, "description": "IST time to start precheck.", "input_type": "time"},
    "execution_time":                   {"category": "dispatcher", "is_secret": False, "description": "IST time to place orders.", "input_type": "time"},

    # Dry run E2E testing
    "dry_run_e2e":            {"category": "testing", "is_secret": False, "description": "Enable E2E dry run: agents run fully but execution uses scripted levels and prices."},
    "dry_run_ticker":         {"category": "testing", "is_secret": False, "description": "Ticker to analyze in dry run (screener skipped)."},
    "dry_run_plan":           {"category": "testing", "is_secret": False, "description": "Hardcoded trade levels used at execution time in dry run (overrides agent output). JSON object."},
    "dry_run_price_sequence": {"category": "testing", "is_secret": False, "description": "Ordered price list fed to monitor ticks in dry run — cycles when exhausted. JSON array."},

    # Telegram notifications
    "telegram_notifications_enabled": {"category": "telegram", "is_secret": False, "description": "Stream live pipeline events (precheck, orders, monitor ticks, exits, EOD) to Telegram."},
    "telegram_bot_token":              {"category": "telegram", "is_secret": True,  "description": "Bot token from @BotFather. Required when notifications are enabled."},
    "telegram_chat_id":                {"category": "telegram", "is_secret": False, "description": "Telegram chat id to send updates to (your user id, group id, or channel id like -100…)."},
    "telegram_startup_message_enabled":     {"category": "telegram", "is_secret": False, "description": "Post a 'Pipeline online' notification every time the FastAPI process boots."},
    "telegram_morning_message_enabled":     {"category": "telegram", "is_secret": False, "description": "Post a daily morning brief (date, today's starting capital, prior-day P&L) at the configured time."},
    "telegram_morning_message_time":        {"category": "telegram", "is_secret": False, "description": "IST time to send the morning brief (HH:MM). Fires from the first dispatcher tick at or after this time each day.", "input_type": "time"},
    "telegram_morning_message_last_date":   {"category": "telegram", "is_secret": False, "description": "INTERNAL — last date the morning brief was sent. Auto-managed; do not edit."},
    "telegram_reports_enabled":             {"category": "telegram", "is_secret": False, "description": "Master toggle for sending analysis report files (per-ticker .md + EOD .zip) to Telegram."},
    "telegram_reports_per_ticker":          {"category": "telegram", "is_secret": False, "description": "Send each ticker's complete_report.md to Telegram as soon as it's generated during precheck."},
    "telegram_reports_eod_zip":             {"category": "telegram", "is_secret": False, "description": "At EOD (in handle_analysis), zip the full reports/<DATE>/ tree and send as one attachment."},
}
