# End-to-End Pipeline Flow

This document explains how the complete intraday trading pipeline works — from stock selection to trade execution to daily reporting.

---

## High-Level Architecture

```
┌─────────────────────────────────────────────────────────────────────────┐
│                         run_pipeline.py                                  │
├─────────┬─────────────┬──────────────┬──────────────────┬───────────────┤
│ Phase 1 │   Phase 2   │   Phase 3    │     Phase 4      │   Phase 5     │
│ Screen  │   Analyze   │   Execute    │    Monitor       │   Report      │
│ (08:30) │  (08:30-10) │   (10:30)    │  (10:30-15:15)   │   (15:20)     │
└────┬────┴──────┬──────┴──────┬───────┴────────┬─────────┴───────┬───────┘
     │           │             │                │                 │
     ▼           ▼             ▼                ▼                 ▼
 Screener   TradingAgents   PaperTrader    MarketMonitor       SQLite DB
             Graph (LLM)                    (yfinance)        (web dashboard)
```

---

## Phase 1: Stock Screening

**Entry point**: `run_pipeline.py:run_screener(top_n=5)`

**Input**: Hardcoded universe of 149 NSE tickers (`tradingagents/screener/universe.py`)

**Process**:

1. Takes the first 80 tickers from `NSE_MIDCAP_SMALLCAP_UNIVERSE`
2. For each ticker, fetches 30 days of OHLCV from yfinance
3. Applies three filters (`tradingagents/screener/filters.py`):
   - **Liquidity**: avg daily turnover >= Rs.5 Crores
   - **Volatility**: 14-day ATR >= 1.5% of price
   - **Price band**: Rs.50 - Rs.50,000
4. Stocks that pass all filters go to the Ranker (`tradingagents/screener/ranker.py`)
5. Ranker computes composite score: `0.4*momentum + 0.3*volume + 0.3*volatility`
   - Momentum: 5-day and 20-day returns (favours upward trend)
   - Volume: recent 5-day avg vs historical avg (favours surging interest)
   - Volatility: prefers 1.5-5% ATR (sweet spot for intraday)
6. Returns top N stocks sorted by composite score

**Output**: `List[dict]` — each dict has `ticker`, `price`, `avg_volume_inr_crores`, `atr_pct`, `composite_score`

---

## Phase 2: Multi-Agent Analysis

**Entry point**: `run_pipeline.py:run_analysis_phase(top_stocks)`

**Input**: Top 5 screened stocks from Phase 1

**For each stock**, calls `TradingAgentsGraph.propagate(ticker, date)`:

### The Agent Graph (LangGraph StateGraph)

```
                    ┌───────────────────────────────────┐
                    │        Initial State              │
                    │  (ticker, date, memory context)   │
                    └───────────────┬───────────────────┘
                                    │
                    ┌───────────────▼───────────────────┐
                    │      6 Analyst Agents (parallel)   │
                    ├───────────────────────────────────┤
                    │ 1. Market Analyst                  │
                    │    - get_stock_data (OHLCV)        │
                    │    - get_indicators (SMA/EMA/RSI)  │
                    │                                    │
                    │ 2. Retail Sentiment Analyst        │
                    │    - get_news                      │
                    │                                    │
                    │ 3. Institutional Sentiment Analyst │
                    │    - get_news                      │
                    │    - get_fii_dii_data (NEW)        │
                    │                                    │
                    │ 4. Contrarian Sentiment Analyst    │
                    │    - get_news                      │
                    │                                    │
                    │ 5. News Analyst                    │
                    │    - get_news + get_global_news    │
                    │    - get_insider_transactions      │
                    │                                    │
                    │ 6. Fundamentals Analyst            │
                    │    - get_fundamentals              │
                    │    - get_balance_sheet/cashflow    │
                    │    - get_income_statement          │
                    └───────────────┬───────────────────┘
                                    │ 6 reports
                    ┌───────────────▼───────────────────┐
                    │    Investment Debate (Bull vs Bear) │
                    │    - Bull argues for buying         │
                    │    - Bear argues for selling        │
                    │    - Judge decides winner           │
                    │    (max_debate_rounds = 1)          │
                    └───────────────┬───────────────────┘
                                    │
                    ┌───────────────▼───────────────────┐
                    │      Research Manager              │
                    │  Synthesizes all reports + debate  │
                    │  into a unified research brief     │
                    └───────────────┬───────────────────┘
                                    │
                    ┌───────────────▼───────────────────┐
                    │          Trader                    │
                    │  Proposes entry/SL/targets based   │
                    │  on Research Manager's brief       │
                    └───────────────┬───────────────────┘
                                    │
                    ┌───────────────▼───────────────────┐
                    │  Risk Debate (3 debaters)          │
                    │  - Aggressive: "add position"      │
                    │  - Conservative: "reduce risk"     │
                    │  - Neutral: balanced view          │
                    │  - Judge decides final sizing      │
                    │  (max_risk_discuss_rounds = 1)     │
                    └───────────────┬───────────────────┘
                                    │
                    ┌───────────────▼───────────────────┐
                    │      Portfolio Manager (PM)        │
                    │  Makes final call:                 │
                    │  - Rating: Buy/Overweight/Hold/    │
                    │            Underweight/Sell        │
                    │  - Entry Zone, Stop Loss, Targets  │
                    │  - Confidence /10, Position Size % │
                    │  - Skip Rule, Thesis              │
                    └───────────────┬───────────────────┘
                                    │
                                    ▼
                    (final_state dict, rating string)
```

### LLM Configuration

| Role                            | Model                       | Provider |
| ------------------------------- | --------------------------- | -------- |
| Analysts, Debates, Research Mgr | `kimi-k2.5` (quick)         | Moonshot |
| Trader, Portfolio Manager       | `kimi-k2.6` (deep thinking) | Moonshot |

The deep-think model (K2.6) has thinking mode enabled. Since Moonshot's API rejects `tool_choice` with thinking active, the Trader and PM use **free-text generation** (no structured output binding). Their markdown output is parsed by `plan_extractor.py` using regex.

### Plan Extraction

After `propagate()` returns, `plan_extractor.py` parses the PM's markdown output:

```
**Entry Zone**: ₹6,420 - ₹6,480    →  entry_zone_low=6420, entry_zone_high=6480
**Stop Loss**: ₹6,350              →  stop_loss=6350
**Target 1**: ₹6,550               →  target_1=6550
**Target 2**: ₹6,700               →  target_2=6700
**Confidence**: 7/10                →  confidence_score=7
**Position Size %**: 20%            →  position_size_pct=20
**Skip Rule**: Don't enter after 11:30  →  skip_rule="Don't enter after 11:30"
```

Falls back to Trader output if PM lacks levels, then to free-text regex patterns.

### Database Persistence
hello 
Regardless of the rating, Phase 2 saves to SQLite:

- **trade_plans**: ticker, date, rating, entry/SL/targets, thesis
- **agent_reports**: each analyst's report (Market, Sentiment×3, News, Fundamentals, PM)
- **debates**: bull/bear arguments, judge verdict, confidence

### Filtering for Execution

Only plans with rating = `Buy` or `Overweight` AND valid entry_zone + stop_loss proceed to Phase 3. Hold/Underweight/Sell are tracked in DB but not traded.

**Output**: `List[dict]` — actionable trade plans

---

## Phase 3: Order Execution

**Entry point**: `run_pipeline.py:run_execution_phase(plans, paper_trader)`

**Input**: Actionable trade plans from Phase 2

**Process** (`tradingagents/execution/paper_trader.py`):

For each plan, `PaperTrader.place_trade_plan(plan)`:

1. Checks if trading is paused (daily loss limit hit)
2. Calculates quantity:
   - Max capital per stock = 25% of total (Rs.5,000 from Rs.20,000)
   - Max loss per trade = 1.5% of capital (Rs.300)
   - `qty = max_loss_inr / (entry_high - stop_loss)`
   - Capped by max_capital: `qty = max_capital / entry_high`
3. Creates an `Order` object with zones, SL, targets
4. Places order into `OrderManager` (state = PENDING)

**Output**: List of order IDs (strings)

---

## Phase 4: Market Monitoring

**Entry point**: `run_pipeline.py:run_monitoring_phase(paper_trader)`

**Input**: PaperTrader with pending orders

**Process** (`tradingagents/pipeline/market_monitor.py`):

Runs from current time until 15:15 IST:

```
Every 120 seconds:
│
├── Get tracked tickers (pending orders + open positions)
├── For each ticker:
│   └── yf.Ticker(t).fast_info["lastPrice"]
│
├── Feed each price into paper_trader.on_price_tick(ticker, price, time)
│   │
│   ├── Check pending orders:
│   │   └── If price is within entry_zone_low-high → FILL order → open position
│   │
│   └── Check open positions:
│       ├── price <= stop_loss → EXIT (loss), log event
│       ├── price >= target_1 → PARTIAL EXIT 50%, move SL to breakeven
│       └── price >= target_2 → FULL EXIT (profit), log event
│
└── Persist events to SQLite (insert_position)

At 15:15 IST:
└── hard_exit_all() → close all remaining positions at market price
```

### Risk Controls (checked every tick)

| Rule               | Limit                  | Action                    |
| ------------------ | ---------------------- | ------------------------- |
| Max loss per trade | 1.5% of capital        | Built into stop-loss      |
| Daily loss limit   | 3% of capital (Rs.600) | Pause all trading         |
| Weekly loss limit  | 5% of capital          | Warning                   |
| Hard exit time     | 15:15 IST              | Force close everything    |
| Skip rule time     | 11:30 IST              | Don't enter new positions |

**Output**: All positions closed by 15:15

---

## Phase 5: Daily Reporting

**Entry point**: `run_pipeline.py:run_reporting_phase(paper_trader)`

**Input**: PaperTrader with completed trades

**Process**:

1. Reads metrics from `paper_trader.get_state()["metrics"]`:
   - current_capital, daily_pnl, total_return_pct
   - total_trades, winning_trades, win_rate
   - max_drawdown_pct
2. Saves to SQLite via `insert_daily_metrics()`
3. Web dashboard reads from SQLite to display performance

**Output**: Row in `daily_metrics` table

---

## Data Flow Summary

```
NSE Universe (149 tickers, hardcoded in screener/universe.py)
    │
    │ ScreenFilters: liquidity ≥ ₹5Cr, ATR ≥ 1.5%, price ₹50-50K
    │ Uses: yfinance 30-day OHLCV
    ▼
~20-30 stocks pass filters
    │
    │ Ranker: composite_score = 0.4*momentum + 0.3*volume + 0.3*volatility
    ▼
Top 5 stocks (by composite score)
    │
    │ TradingAgentsGraph.propagate(ticker, date) × 5 (sequential)
    │ Uses: Kimi K2.5/K2.6 via Moonshot API
    │ Duration: ~3-5 min per stock = ~15-25 min total
    ▼
5 × (final_state, rating)
    │
    │ plan_extractor.extract_trade_plan() — regex parse PM markdown
    │ → All saved to SQLite (trade_plans, agent_reports, debates)
    ▼
N actionable plans (only Buy/Overweight with valid levels)
    │
    │ PaperTrader.place_trade_plan()
    │ qty = min(max_loss/risk, max_capital/price)
    ▼
Pending orders (waiting for entry zone hit)
    │
    │ MarketMonitor polls yfinance every 2 min (10:30 → 15:15)
    │ → on_price_tick() checks entry zones → fills
    │ → Monitors SL/targets → exits
    ▼
Filled → Monitored → Exited (by SL, Target, or Hard Exit)
    │
    │ All events → SQLite (positions table)
    ▼
15:15 hard_exit_all()
    │
    │ insert_daily_metrics()
    ▼
SQLite DB → Web Dashboard (run_web.py)
```

---

## Automation (daily_runner.py)

For hands-free operation, `daily_runner.py` uses APScheduler:

| Time (IST) | Job                         | What it does                                           |
| ---------- | --------------------------- | ------------------------------------------------------ |
| 08:30      | `job_screen_and_analyze()`  | Screen 80 stocks, rank top 5, run multi-agent analysis |
| 10:30      | `job_execute_and_monitor()` | Place orders, poll prices until 15:15                  |
| 15:20      | `job_daily_report()`        | Save metrics to DB                                     |

Jobs share state via a module-level `_daily_state` dict (plans from 08:30 are used at 10:30).

```bash
# Start scheduler (runs forever, Mon-Fri only)
python -m tradingagents.pipeline.daily_runner

# Test all jobs once sequentially
python -m tradingagents.pipeline.daily_runner --once
```

---

## Key Files Reference

| File                                          | Purpose                                 |
| --------------------------------------------- | --------------------------------------- |
| `run_pipeline.py`                             | Main orchestrator (5 phases)            |
| `tradingagents/screener/universe.py`          | Hardcoded 149 NSE tickers               |
| `tradingagents/screener/filters.py`           | Liquidity/ATR/price filters             |
| `tradingagents/screener/ranker.py`            | Composite scoring and ranking           |
| `tradingagents/graph/trading_graph.py`        | LangGraph agent orchestration           |
| `tradingagents/graph/setup.py`                | StateGraph wiring (nodes + edges)       |
| `tradingagents/agents/`                       | All agent implementations               |
| `tradingagents/pipeline/plan_extractor.py`    | Regex parse PM output → trade plan dict |
| `tradingagents/pipeline/fii_dii.py`           | MoneyControl FII/DII scraping           |
| `tradingagents/pipeline/market_monitor.py`    | yfinance price polling loop             |
| `tradingagents/pipeline/daily_runner.py`      | APScheduler automation                  |
| `tradingagents/execution/paper_trader.py`     | Paper trading engine                    |
| `tradingagents/execution/order_manager.py`    | Order state machine                     |
| `tradingagents/execution/position_tracker.py` | Position P&L tracking                   |
| `tradingagents/web/database.py`               | SQLite schema + insert functions        |
| `tradingagents/web/app.py`                    | Flask web dashboard                     |
| `tradingagents/default_config.py`             | All configurable parameters             |
| `tradingagents/llm_clients/openai_client.py`  | Moonshot/DeepSeek LLM client            |

---

## Configuration (`default_config.py`)

```python
"llm_provider": "moonshot"           # LLM provider
"deep_think_llm": "kimi-k2.6"       # For Trader + PM (thinking mode)
"quick_think_llm": "kimi-k2.5"      # For Analysts + Debates (fast)
"initial_capital": 20000             # Rs.20K paper trading capital
"max_capital_per_stock_pct": 25      # Max 25% per stock
"max_loss_per_trade_pct": 1.5        # Max 1.5% loss per trade
"daily_loss_limit_pct": 3.0          # Pause at 3% daily loss
"hard_exit_time": "15:15"            # Force close all positions
"max_debate_rounds": 1               # Bull/Bear debate rounds
"max_risk_discuss_rounds": 1         # Risk debate rounds
```

---

## Quick Start

```bash
source .venv/bin/activate

# Full pipeline (skip market-open check for testing)
python run_pipeline.py --skip-market-check --top-n 1

# Analysis only (no orders, no monitoring)
python run_pipeline.py --skip-market-check --analyze-only --top-n 3

# Web dashboard
python run_web.py
# → http://localhost:5000

# Automated daily (leave running)
python -m tradingagents.pipeline.daily_runner
```
