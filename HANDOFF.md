# Claude Session Handoff — agent-p Trading Pipeline

**Date:** 2026-05-11/12  
**Project:** `/Users/aramteke/ar-codes/agent-p` — NSE India intraday paper trading pipeline  
**Stack:** Python (FastAPI + LangGraph + APScheduler), React frontend, SQLite, Docker, Kimi K2 (Moonshot) LLM

---

## Goal of the Session

Fix a live trading pipeline that was broken in multiple ways after first deployment:
1. Pipeline re-running precheck endlessly
2. All trade plans showing null confidence/entry/SL/T1 values
3. UI state badge frozen ("precheck 8:00am") — never updating
4. Build a Force Rerun button for manual reset
5. Add proper logging visible in `docker logs`
6. Fix Kimi K2 structured output failures
7. Add live-price adjustment at execution time
8. Build a full E2E dry run testing mode

---

## Architecture Overview

```
APScheduler (60s tick)
  └── dispatch_pipeline()
        ├── idle → precheck (08:10 IST)
        │     └── run_screener() → run_analysis_phase() [LangGraph multi-agent]
        │           └── RM → Trader → PM → plan_extractor → DB
        ├── precheck → waiting (after analysis done)
        │     └── run_execution_phase() [place orders on PaperTrader]
        ├── waiting → monitor (09:30 IST)
        │     └── MarketMonitor.tick() every 600s [yfinance prices → SL/T1 checks]
        ├── monitor → analysis (15:15 IST hard exit)
        │     └── run_reporting_phase() + run_eod_reflection()
        └── analysis → idle
```

State persisted in SQLite (`pipeline_state` + `pipeline_state_history` tables).  
Config in `app_config` table, editable via Settings UI.  
Container: `trade-fish` on DigitalOcean droplet.

---

## Code Changes Summary

### 1. Kimi K2 Structured Output Fix (ROOT CAUSE of null plans)

**Problem:** `MoonshotChatOpenAI.with_structured_output()` was tried with `function_calling` method → API 400 error (`tool_choice incompatible with thinking enabled`). Then tried `json_mode` → model returned intermediate thinking JSON `{"type":"response","status":"pending",...}` and ignored JSON format for SKIP decisions.

**Fix:** Block all `kimi-k2*` from structured output (raise `NotImplementedError`), route to free-text path, and add format suffix to guide the model to output exact markdown headers the regex extractor looks for.

**Files changed:**
- [`tradingagents/llm_clients/openai_client.py`](tradingagents/llm_clients/openai_client.py) — `MoonshotChatOpenAI._THINKING_MODELS = ("kimi-k2",)` raises `NotImplementedError` for all K2 variants
- [`tradingagents/agents/utils/structured.py`](tradingagents/agents/utils/structured.py) — Added `freetext_suffix` param + `_append_suffix()` helper to `invoke_structured_or_freetext()`
- [`tradingagents/agents/managers/portfolio_manager.py`](tradingagents/agents/managers/portfolio_manager.py) — Added `_PM_FREETEXT_FORMAT` constant with exact header names (`**Rating**`, `**Entry Zone**: ₹X - ₹Y`, `**Confidence**: N/10`, etc.)
- [`tradingagents/agents/trader/trader.py`](tradingagents/agents/trader/trader.py) — Added `_TRADER_FREETEXT_FORMAT`
- [`tradingagents/agents/managers/research_manager.py`](tradingagents/agents/managers/research_manager.py) — Added `_RM_FREETEXT_FORMAT`

**Expected logs (healthy):**
```
[INFO] structured: Research Manager: using free-text generation (structured output unavailable for this model)
[INFO] plan_extractor: CGPOWER.NS/2026-05-11: all fields extracted (conf=7)
```

---

### 2. Idle→Precheck Guard (prevents infinite re-runs)

**Problem:** `handle_idle()` only checked clock. After any state returned to idle (including after analysis or config change), it immediately re-entered precheck.

**Fix:**
- [`tradingagents/pipeline/state_machine.py`](tradingagents/pipeline/state_machine.py) — Added `has_completed_today(trade_date, stage)` — checks `pipeline_state_history` for `from_state=precheck` on today's date
- [`tradingagents/pipeline/dispatcher.py`](tradingagents/pipeline/dispatcher.py) — `handle_idle()` calls `sm.has_completed_today(today, "precheck")` before allowing re-entry

---

### 3. Background Threading (fixes APScheduler skip + frozen badge)

**Problem:** `handle_precheck`/`handle_waiting`/`handle_analysis` blocked the 60s tick for 15-25min. APScheduler skipped all subsequent ticks → `state_since` never updated → UI badge frozen.

**Fix:** [`tradingagents/pipeline/dispatcher.py`](tradingagents/pipeline/dispatcher.py) — Full rewrite of `dispatch_pipeline()`:
- Long-running states (`precheck`, `waiting`, `analysis`) offloaded to `ThreadPoolExecutor(max_workers=1)`
- Each 60s tick: if bg task running → update `state_since` (keeps badge fresh), return immediately
- `_background_future`, `_background_state`, `_background_started_at` module-level state
- "Still running" logged only every 5 minutes (`int(elapsed) % 300 < 60`)
- Added `_cancel_background()` + `_clear_background()` helpers

---

### 4. Cache-Miss Recovery (container restart robustness)

**Problem:** After container restart, in-memory `_daily_runtime` lost `plans` and `paper_trader`. `handle_waiting` would re-run precheck from scratch; `handle_monitor` would jump to analysis with no trades.

**Fix:** [`tradingagents/pipeline/dispatcher.py`](tradingagents/pipeline/dispatcher.py)
- `handle_waiting`: if `runtime["plans"]` empty, load today's Buy plans from DB via `get_trade_plans(trade_date)` before falling back to precheck
- `handle_monitor`: if `paper_trader` is None, rebuild from DB plans + call `run_execution_phase()` to re-place orders

---

### 5. Force Rerun Endpoint + UI Button

**Files:**
- [`tradingagents/web/routes/pipeline.py`](tradingagents/web/routes/pipeline.py) — `POST /api/pipeline/force-rerun`: deletes today's `trade_plans`, `agent_reports`, `debates`, `pipeline_state_history` rows + removes markdown report files + cancels bg task + transitions to `precheck`
- [`frontend/src/api.js`](frontend/src/api.js) — `forceRerun()` export
- [`frontend/src/components/PipelineStateBadge.jsx`](frontend/src/components/PipelineStateBadge.jsx) — "Force Rerun" button with confirmation dialog, visible when state is `idle`/`waiting`/`holiday`

---

### 6. Live Price Adjustment at Execution Time

**Problem:** Agents analyze at 08:10 using yesterday's daily close. By 09:30 execution, stock may have gapped 2-3%. Order sits PENDING until 11:30 skip rule expires it.

**Fix:** [`run_pipeline.py`](run_pipeline.py) — In `run_execution_phase()`, before placing each order:
- `_get_live_price(ticker)` — fetches yfinance 1m close
- `_adjust_plan_to_live_price(plan, live_price)` — if gap > 1%, shifts entry zone + SL + targets by the delta (preserves zone width and R:R)
- Updates DB row via `update_trade_plan_levels(plan, gap_pct)`
- Config constant `_EXEC_PRICE_ADJUST_THRESHOLD_PCT = 1.0`

**DB change:** [`tradingagents/web/database.py`](tradingagents/web/database.py) — Added `price_adjusted_pct REAL DEFAULT 0` column to `trade_plans`. Migration in `init_db()`.

**UI:** [`frontend/src/pages/Today.jsx`](frontend/src/pages/Today.jsx) — Shows `↑2.5% adj` badge (amber = up, blue = down) next to ticker name when adjusted.

---

### 7. Null Display Fix

**Problem:** Plans with null fields rendered as `conf -/10, entry - SL /T1`.

**Fix:** [`frontend/src/pages/Today.jsx`](frontend/src/pages/Today.jsx) + [`frontend/src/pages/HistoryDate.jsx`](frontend/src/pages/HistoryDate.jsx) — Null-safe rendering: `p.confidence_score ?? '–'`, `p.stop_loss != null ? p.stop_loss.toFixed(1) : '–'`

---

### 8. Logging Improvements

- [`run_web.py`](run_web.py) — `logging.basicConfig(level=INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")`. Quieted `apscheduler.executors.default` to WARNING, `uvicorn.access` to WARNING.
- [`tradingagents/execution/order_manager.py`](tradingagents/execution/order_manager.py) — `[order] PLACED/FILLED/EXPIRED/EXIT TRIGGER` logs
- [`tradingagents/agents/managers/portfolio_manager.py`](tradingagents/agents/managers/portfolio_manager.py) — `[PM] TICKER: invoking Portfolio Manager` + decision preview
- [`tradingagents/agents/managers/research_manager.py`](tradingagents/agents/managers/research_manager.py) — `[RM] TICKER: invoking Research Manager` + plan preview
- [`tradingagents/agents/trader/trader.py`](tradingagents/agents/trader/trader.py) — `[Trader] TICKER: invoking Trader` + proposal preview
- [`tradingagents/pipeline/allocator.py`](tradingagents/pipeline/allocator.py) — `[allocator] #N TICKER conf=X R:R=Y size=Z%`
- [`tradingagents/execution/risk_manager.py`](tradingagents/execution/risk_manager.py) — `[risk] TICKER SL old → new (reason, unrealized%)`
- [`tradingagents/pipeline/plan_extractor.py`](tradingagents/pipeline/plan_extractor.py) — Warning when null fields extracted
- [`run_pipeline.py`](run_pipeline.py) — Warning when plan saved to DB with missing fields

---

### 9. Dry Run E2E Testing Mode

**Purpose:** Test full pipeline end-to-end at any time of day without real market data or polluting trade history.

**Design:**
- Agents run fully (real LLM calls — validates the discussion works)
- Screener skipped — uses `dry_run_ticker` directly
- Execution overrides agent levels with `dry_run_plan` (hardcoded predictable levels)
- Monitor uses `dry_run_price_sequence` (JSON array) instead of yfinance — cycles through scripted prices
- Monitor interval auto-set to 60s in dry run
- `is_execution_window()` gate bypassed in dry run
- All DB rows tagged `is_dry_run=1`

**Config keys added** ([`tradingagents/default_config.py`](tradingagents/default_config.py)):
- `dry_run_e2e: bool` — master toggle
- `dry_run_ticker: str` — e.g. `"RELIANCE.NS"`
- `dry_run_plan: dict` — entry/SL/T1/T2/confidence/position_size override
- `dry_run_price_sequence: list` — e.g. `[1395, 1402, 1408, 1415, 1422, 1435, 1441, 1450]`

**Files changed:**
- [`tradingagents/execution/order_manager.py`](tradingagents/execution/order_manager.py) — `_is_force_fill()` reads `dry_run_e2e` config instead of env var (removed `PAPER_TRADE_FORCE_FILL` env var entirely)
- [`tradingagents/pipeline/market_monitor.py`](tradingagents/pipeline/market_monitor.py) — `_fetch_dry_run_prices()`, `_dry_run_price_idx` counter, `_reload_config()` picks up `dry_run_e2e`
- [`tradingagents/pipeline/dispatcher.py`](tradingagents/pipeline/dispatcher.py) — `handle_precheck` injects dry ticker; `handle_monitor` forces 60s interval
- [`run_pipeline.py`](run_pipeline.py) — `run_execution_phase()` overrides plan levels in dry run
- [`tradingagents/web/database.py`](tradingagents/web/database.py) — `is_dry_run` column on `trade_plans` + `positions`, migration, `insert_trade_plan`/`insert_position` write flag
- [`frontend/src/components/SettingsForm.jsx`](frontend/src/components/SettingsForm.jsx) — Toggle button + ticker input in yellow "Dry Run E2E Testing" section
- [`frontend/src/pages/Today.jsx`](frontend/src/pages/Today.jsx) — "DRY RUN" badge (amber) on plan cards

**How to use:**
1. Settings → enable "Dry Run E2E" toggle → set ticker
2. Force Rerun
3. Watch logs: `[precheck] DRY RUN` → agents run → `[execution] DRY RUN: overriding levels` → `[monitor] DRY RUN: price tick #N = ₹X` → fill → exit

---

### 10. Minor Fixes

- [`tradingagents/default_config.py`](tradingagents/default_config.py) — Fixed invalid model IDs: `claude-opus-4-0-20250115` → `claude-opus-4-7`, `claude-haiku-4-5-20251001` → `claude-haiku-4-5`
- [`tradingagents/llm_clients/model_catalog.py`](tradingagents/llm_clients/model_catalog.py) — Added `claude-opus-4-7` to anthropic model list
- [`frontend/src/components/SettingsForm.jsx`](frontend/src/components/SettingsForm.jsx) — Moved "Reset Paper Capital" button from Home page to Settings bottom (red color)
- [`frontend/src/components/OnDemandList.jsx`](frontend/src/components/OnDemandList.jsx) — Added duration column to recent analyses list (trigger time → time taken)
- [`docker-compose.yml`](docker-compose.yml) — Removed `PAPER_TRADE_FORCE_FILL` env var entirely

---

## Errors Encountered & Fixes

| Error | Root Cause | Fix |
|---|---|---|
| `tool_choice 'specified' is incompatible with thinking enabled` | `function_calling` method sends `tool_choice` which Kimi K2 rejects when thinking is on | Block all `kimi-k2*` from structured output, use free-text path |
| `OutputParserException: Failed to parse from {"type":"response","status":"pending"}` | `json_mode` on Kimi K2 leaks intermediate thinking JSON | Same fix as above |
| `OutputParserException: Invalid json output: **SKIP**` | Model ignores `json_mode` for certain outputs | Same fix |
| `APScheduler: maximum number of running instances reached (1)` | Long-running handlers blocked 60s tick | Background threading via ThreadPoolExecutor |
| Plans showing `conf -/10, entry - SL /T1` | All null because structured output failed, free-text regex couldn't parse unstructured output | Free-text format suffix with exact markdown headers |
| State badge frozen at "precheck 8:00am" | `state_since` never updated while bg task blocked | BG threading: each tick updates `state_since` |
| Precheck re-runs after config change | `handle_idle` only checked clock | `has_completed_today()` DB guard |
| Container restart → precheck re-runs from scratch | In-memory `_daily_runtime` lost, fallback went to full precheck | Reload plans from DB on cache miss |
| Entry zone stale (agent says ₹838, stock opened at ₹862) | Agents use yesterday's daily OHLCV; by execution time price gapped | Live price fetch at execution time, shift zone if >1% gap |
| `[waiting] no cached plans; rerunning precheck` | Same cache-miss issue after restart | DB reload before fallback to precheck |

---

## Key File Map

```
run_pipeline.py              ← Phase 1-5 orchestration, execution phase, live price adjustment
run_web.py                   ← Uvicorn entrypoint, logging config
tradingagents/
  pipeline/
    dispatcher.py            ← APScheduler cron, state machine handlers, bg threading
    state_machine.py         ← SQLite state read/write, has_completed_today()
    market_monitor.py        ← yfinance price polling, trailing stops, dry run price feed
    plan_extractor.py        ← Regex extraction of trade plan from PM/Trader markdown
    allocator.py             ← Cross-stock ranking, capital allocation (confidence × R:R)
  agents/
    utils/structured.py      ← bind_structured(), invoke_structured_or_freetext()
    managers/portfolio_manager.py
    managers/research_manager.py
    trader/trader.py
  execution/
    order_manager.py         ← Order lifecycle, entry/exit checks, force-fill
    paper_trader.py          ← Capital tracking, position management
    risk_manager.py          ← Trailing stop ladder
  llm_clients/
    openai_client.py         ← NormalizedChatOpenAI, MoonshotChatOpenAI, DeepSeekChatOpenAI
  web/
    database.py              ← SQLite schema, all insert/update/query functions
    config_service.py        ← DB-backed config read/write
    routes/pipeline.py       ← Pipeline state API + force-rerun endpoint
  default_config.py          ← Seed defaults + CONFIG_METADATA
frontend/src/
  pages/Today.jsx            ← Today's plans + open positions
  pages/HistoryDate.jsx      ← Per-date history
  components/SettingsForm.jsx ← Config editor + dry run toggle + reset capital
  components/PipelineStateBadge.jsx ← State badge + Force Rerun button
  api.js                     ← All API calls
docker-compose.yml           ← trade-fish service (prod), trade-fish-ollama (optional)
```

---

## Deployment

```bash
# On DigitalOcean droplet
docker compose build --no-cache && docker compose up -d
docker logs -f trade-fish --tail 100

# Check current state
curl http://localhost:8000/api/pipeline/state

# Force rerun
curl -X POST http://localhost:8000/api/pipeline/force-rerun

# Check today's plans
docker exec trade-fish python3 -c "
from tradingagents.web.database import get_conn
conn = get_conn()
rows = conn.execute(\"SELECT ticker, entry_zone_low, entry_zone_high, stop_loss, target_1, confidence_score, is_dry_run FROM trade_plans WHERE date='2026-05-12'\").fetchall()
for r in rows: print(dict(r))
conn.close()
"
```

---

## Known State & Next Steps

- **Kimi K2 free-text path is working** — logs show INFO "using free-text generation", not WARNING
- **Plan extraction** — watch for `[plan_extractor] TICKER: all fields extracted (conf=X)` to confirm regex parsing works on next real run
- **Tomorrow's live run** — first real test of: precheck runs once (guard works), live price adjustment shifts zone, orders fill, monitor tracks positions, hard exit at 15:15
- **Dry run** — built but not yet tested end-to-end in production; toggle in Settings → Force Rerun to test
- **`deploy_pct_top_k`** default 70% — 30% kept as dry powder buffer intentionally
