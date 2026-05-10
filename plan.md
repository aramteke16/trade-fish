# Intraday Trading Agent — Final Plan (Fork of TradingAgents)

## Context

Build a personal intraday trading agent for Indian mid/small-cap equities (NSE). We fork **TradingAgents** (github.com/TauricResearch/TradingAgents, 70k stars, Apache 2.0) as the foundation and customize it for:
- Indian market (NSE mid/small caps)
- 3 sentiment sub-agents (retail, institutional, contrarian)
- Kimi K2.6 + Claude as dual LLM providers
- Intraday execution (10:30 AM - 3:15 PM)
- Paper trading with ₹20,000 simulated capital
- Daily compounding
- **Simple web UI** to visualize debates, trade plans, and P&L

**Target:** 1% daily (aspirational). Realistic floor: 0.2-0.3% daily.

---

## What TradingAgents Already Provides (We Keep)

| Component | How It Works |
|-----------|-------------|
| Bull/Bear debate engine | Configurable N-round debate with LangGraph routing |
| Analyst agents | Market, Fundamentals, News, Social Media (4 agents) |
| Risk debate | Aggressive vs Conservative vs Neutral (3 debaters) |
| Trader agent | Synthesizes into Buy/Hold/Sell proposal with entry/stop/sizing |
| Portfolio Manager | Final decision with rating + thesis |
| LLM provider system | Factory pattern, supports Claude/OpenAI/DeepSeek/Qwen/Ollama + more |
| yfinance integration | Works with `.NS` tickers for NSE stocks |
| LangGraph orchestration | StateGraph with conditional edges, checkpoint/resume |
| Memory system | Per-ticker history, reflection after each trade |
| Structured outputs | Pydantic schemas for all agent responses |

---

## What We Add/Modify

### 1. Add Kimi K2.6 as LLM Provider
- Moonshot API is OpenAI-compatible (same chat/completions endpoint)
- Add to `llm_clients/factory.py` and `model_catalog.py`
- Base URL: `https://api.moonshot.cn/v1`
- Use for: analysis agents (cheaper, structured tasks)

### 2. Replace Social Media Agent → 3 Sentiment Sub-Agents
- Remove existing `social_media_analyst.py`
- Add 3 new agents in `agents/analysts/`:
  - `sentiment_retail.py` — retail investor lens (social buzz, retail momentum)
  - `sentiment_institutional.py` — smart money lens (FII/DII flows, block deals, MF holdings)
  - `sentiment_contrarian.py` — what if consensus is wrong?
- Wire all 3 into LangGraph as parallel nodes

### 3. Add Indian Stock Screener (NEW)
- **Location:** `tradingagents/screener/`
- Screen ~400 mid/small cap stocks → shortlist top 15-20 → agents analyze → final 4-5
- Filters: liquidity (>₹5Cr daily vol), volatility (ATR>1.5%), price (>₹50), no circuits
- Data: jugaad-data + yfinance

### 4. Add Indian Market Data Layer
- **Location:** `tradingagents/dataflows/indian_market.py`
- **Historical/screening data:** yfinance (`.NS` tickers) — batch OHLCV fetch for 400 stocks during morning pipeline
- **Live intraday data:** Angel One SmartAPI — persistent WebSocket stream for 4-5 selected tickers during 10:30 AM–3:15 PM execution window (no polling, event-driven)
- Indian news (MoneyControl RSS, Economic Times RSS)
- FII/DII flow data from NSE
- NSE holiday calendar, IST timezone handling

> **Data split rationale:** yfinance is reliable for EOD/historical batch pulls (screening, backtesting, technicals). Angel SmartAPI is free, actively maintained, and provides WebSocket ticks — necessary for real-time stop-loss and target monitoring during paper trading.

### 5. Add Paper Trading Execution Engine (NEW)
- **Location:** `tradingagents/execution/`
- Simulated orders with realistic fills
- Entry zone monitoring, stop-loss, dual targets
- Hard exit by 3:15 PM
- Daily P&L + capital compounding (₹20K start)

### 6. Add Scheduling & Automation
- APScheduler (IST-aware)
- Morning pipeline: 9:00 AM
- Market monitor: 10:30 AM - 3:15 PM (check prices every 1 min)
- EOD report: 3:30 PM

### 7. Add Performance Tracking
- Win rate, avg win/loss, Sharpe ratio, max drawdown
- Agent accuracy tracking
- Daily + weekly reports
- SQLite storage

### 8. Extend Output Schema
- `entry_zone_low` / `entry_zone_high` (buy range)
- `target_1` / `target_2` (dual targets)
- `skip_rule` (skip if not in zone by 11:30)
- `confidence_score` (1-10)
- `position_size_pct`

### 9. Multi-Stock Pipeline
- Current: analyzes 1 ticker at a time
- Modified: screen → analyze top 15-20 → debate → rank → pick 4-5
- Parallelize with asyncio

### 10. Web Dashboard (NEW)

**Purpose:** Visual interface to watch agent debates, view trade plans, monitor P&L.

**Tech:** FastAPI (backend) + React or plain HTML/JS (frontend)

**Pages/Views:**

| Page | What It Shows |
|------|--------------|
| **Dashboard (Home)** | Today's trade plan, open positions, daily P&L, capital balance |
| **Debate View** | Live/historical agent debate — Bull vs Bear arguments side-by-side, each agent's reasoning visible, final decision highlighted |
| **Stock Analysis** | Per-stock breakdown: all 6 agent scores, entry/exit levels, confidence |
| **Performance** | Charts: cumulative P&L, daily returns, win rate, drawdown over time |
| **History** | Past trade log: date, stock, entry, exit, P&L, which agent recommended |
| **Settings** | Config: risk limits, universe, LLM provider, API keys |

**Debate View Detail:**
```
┌─────────────────────────────────────────────────────────┐
│  TATAELXSI.NS — Debate Round 1                          │
├───────────────────────┬─────────────────────────────────┤
│  🐂 BULL AGENT        │  🐻 BEAR AGENT                  │
├───────────────────────┼─────────────────────────────────┤
│  "Strong breakout     │  "P/E ratio at 65x is           │
│   above 200 DMA with  │   unsustainable. Revenue        │
│   volume confirmation.│   growth slowing Q-o-Q.         │
│   RSI at 58 — room    │   Sector rotation risk from     │
│   to run. FII bought  │   IT to banking..."             │
│   ₹200Cr this week."  │                                 │
├───────────────────────┴─────────────────────────────────┤
│  VERDICT: Bull wins (7/10 confidence) → LONG            │
│  Entry: ₹6,420-₹6,480 | SL: ₹6,350 | T1: ₹6,550      │
└─────────────────────────────────────────────────────────┘
```

**Architecture:**
```
FastAPI Backend ←→ SQLite (trades, debates, metrics)
      │
      ├── /api/today — current trade plan
      ├── /api/debates/{date} — debate history
      ├── /api/positions — open positions + live P&L
      ├── /api/performance — metrics over time
      ├── /api/history — past trades
      └── /ws/live — WebSocket for real-time updates

Frontend (simple, single-page)
      │
      ├── Dashboard tab
      ├── Debates tab (side-by-side view)
      ├── Performance tab (charts)
      └── History tab (table)
```

---

## Modified Architecture Flow

```
9:00 AM — STOCK SCREENER  [yfinance — batch historical OHLCV]
    │  Fetch 3-6 months data for 400 stocks
    │  Filter → top 15-20 candidates (~2 min)
    │
    ▼
9:15 AM — AGENT ANALYSIS (parallel, Kimi K2.6)  [yfinance — technicals/fundamentals]
    │  Technical + Fundamental + News + 3 Sentiment Agents
    │  (results saved to DB → visible in UI)
    │
    ▼
9:30 AM — BULL/BEAR DEBATE (Claude)
    │  2 rounds per stock (saved to DB → Debate View in UI)
    │
    ▼
9:40 AM — RISK DEBATE (Claude)
    │  Aggressive ←→ Conservative ←→ Neutral
    │
    ▼
9:50 AM — PORTFOLIO MANAGER (Claude)
    │  Final 4-5 picks with entry zones, SL, targets
    │  (pushed to Dashboard in UI)
    │
    ▼
10:30 AM — OPEN Angel SmartAPI WebSocket  [subscribe to 4-5 tickers only]
    │  Event-driven: price tick → check entry zone → fill
    │                             → check SL/targets → exit
    │  Live P&L pushed to UI via WebSocket
    │
3:15 PM — HARD EXIT all positions, close WebSocket
    │
    ▼
3:30 PM — DAILY REPORT (visible in Performance tab)
```

---

## Risk Management Rules

| Rule | Value |
|------|-------|
| Max capital per stock | 25% (₹5,000) |
| Max loss per trade | 1.5% of capital (₹300) |
| Daily loss limit | 3% of capital (₹600) → stop trading |
| Weekly loss limit | 5% of capital (₹1,000) → pause & review |
| Trailing stop | Move SL to breakeven at +0.5% |
| Partial exit | 50% at Target 1, 50% at Target 2 |
| Hard exit | 3:15 PM |
| Skip rule | Not in entry zone by 11:30 → skip |
| Sector limit | Max 2 stocks from same sector |
| Min liquidity | Avg daily volume > ₹5 Crore |

---

## Tech Stack

| Component | Technology |
|-----------|-----------|
| Base framework | TradingAgents (forked, Apache 2.0) |
| Orchestration | LangGraph |
| LLM (analysis) | Moonshot Kimi K2.6 |
| LLM (debate/decision) | Claude (Anthropic) |
| Data - Historical/Screening | yfinance (`.NS`) — batch EOD OHLCV for 400 stocks |
| Data - Live intraday | Angel One SmartAPI — WebSocket stream (10:30 AM–3:15 PM) |
| Data - News | MoneyControl RSS, ET RSS |
| Data - FII/DII | NSE data via jugaad-data |
| Technical indicators | stockstats + pandas-ta |
| Web Backend | FastAPI |
| Web Frontend | React (Vite) or plain HTML + Chart.js |
| Real-time | WebSocket (FastAPI) |
| Scheduling | APScheduler (IST) |
| Storage | SQLite |
| Notifications | Telegram bot (optional) |

---

## Project Structure (additions to TradingAgents)

```
tradingagents/               # (existing — forked)
├── agents/analysts/
│   ├── sentiment_retail.py         # NEW
│   ├── sentiment_institutional.py  # NEW
│   └── sentiment_contrarian.py     # NEW
├── dataflows/
│   └── indian_market.py            # NEW
├── screener/                        # NEW
│   ├── universe.py
│   ├── filters.py
│   └── ranker.py
├── execution/                       # NEW
│   ├── paper_trader.py
│   ├── order_manager.py
│   └── position_tracker.py
├── scheduler/                       # NEW
│   └── daily_runner.py
├── tracking/                        # NEW
│   ├── metrics.py
│   ├── reporter.py
│   └── agent_accuracy.py
└── web/                             # NEW
    ├── app.py                       # FastAPI app
    ├── routes/
    │   ├── dashboard.py
    │   ├── debates.py
    │   ├── positions.py
    │   ├── performance.py
    │   └── history.py
    ├── websocket.py                 # Live updates
    └── frontend/
        ├── index.html
        ├── src/
        │   ├── App.jsx
        │   ├── pages/
        │   │   ├── Dashboard.jsx
        │   │   ├── Debates.jsx
        │   │   ├── Performance.jsx
        │   │   └── History.jsx
        │   └── components/
        │       ├── DebateCard.jsx
        │       ├── TradeCard.jsx
        │       ├── PnLChart.jsx
        │       └── PositionTable.jsx
        └── package.json
```

---

## Implementation Order

### Phase 1: Fork & Setup
1. Fork TradingAgents, set up local dev environment
2. Verify base system works (run with a US stock)
3. Add Kimi K2.6 as LLM provider

### Phase 2: Indian Market Adaptation
4. Add yfinance `.NS` integration for historical batch pulls (screening + backtesting)
5. Add Angel One SmartAPI integration for live WebSocket ticks (execution window)
6. Add stock universe (Midcap 150 + Smallcap 250)
7. Add screening/filtering pipeline
8. Add Indian news + FII/DII data sources

### Phase 3: Sentiment Agents
8. Build 3 sentiment sub-agents
9. Wire into LangGraph
10. Test with sample Indian stocks

### Phase 4: Multi-Stock Pipeline
11. Modify pipeline to process 15-20 stocks
12. Add final ranking logic
13. Extend output schema (entry zones, dual targets, skip rules)

### Phase 5: Web Dashboard
14. FastAPI backend + API routes
15. SQLite schema for debates, trades, metrics
16. Frontend: Dashboard + Debate View + Performance + History
17. WebSocket for live position updates

### Phase 6: Execution Engine
18. Paper trading engine (simulated orders)
19. Order manager (entry zones, stop-loss, targets)
20. Market monitor (price checks 10:30-3:15)
21. Daily compounding logic

### Phase 7: Scheduling & Tracking
22. APScheduler for daily automation
23. Performance metrics + reporting
24. Agent accuracy tracking
25. NSE holiday calendar

### Phase 8: Validation
26. Backtest on 3-6 months historical data
27. Paper trade live for 1-2 months
28. Analyze, tune, decide on going live

---

## Key Files to Modify in TradingAgents

| File | Change |
|------|--------|
| `tradingagents/llm_clients/factory.py` | Add Kimi K2.6 provider |
| `tradingagents/llm_clients/model_catalog.py` | Add Kimi model definitions |
| `tradingagents/agents/analysts/` | Add 3 sentiment agents |
| `tradingagents/agents/schemas.py` | Extend with entry zones, dual targets |
| `tradingagents/graph/setup.py` | Wire new agents into graph |
| `tradingagents/graph/trading_graph.py` | Multi-stock propagation |
| `tradingagents/dataflows/interface.py` | Register Indian market vendor |
| `tradingagents/default_config.py` | Add Indian market config |

---

## Verification

1. **Unit tests:** Each new agent produces valid structured output
2. **Integration test:** Full morning pipeline with 5 sample mid-cap stocks
3. **Data test:** jugaad-data fetches correct data for NSE stocks
4. **UI test:** Dashboard loads, debate view renders correctly
5. **Execution test:** Paper trader simulates entry/exit/SL correctly
6. **Backtest:** Run on 3 months historical data, measure P&L
7. **Live paper:** Run daily for 2 weeks, verify full automation
