# End-to-End Pipeline Flow

This is the source-of-truth doc for how the intraday trading pipeline actually
runs today — from the moment the FastAPI process boots, through the daily
state machine, into order placement, the monitoring loop, capital tracking,
and the UI surfaces that visualise all of it.

If you change a state handler, the capital model, or a DB column,
**update this file in the same PR.**

---

## 1. Bird's-eye view

```
┌──────────────────────────────────────────────────────────────────────────┐
│  FastAPI process                                                         │
│                                                                          │
│  ┌──────────────────────────┐    ┌─────────────────────────────────────┐ │
│  │ APScheduler              │    │ FastAPI HTTP                        │ │
│  │   • dispatch_pipeline()  │    │   /api/today, /api/pipeline/*,      │ │
│  │     every 60s            │    │   /api/capital/log, /api/config/*   │ │
│  └────────────┬─────────────┘    └─────────────────┬───────────────────┘ │
│               │                                    │                     │
│               ▼                                    ▼                     │
│  ┌──────────────────────────────────────────────────────────────────┐    │
│  │ State machine  (pipeline_state row + history)                     │   │
│  │   idle → precheck → waiting → monitor → analysis → idle           │   │
│  │                                  ↓                                │   │
│  │                              holiday (weekend / NSE holiday)      │   │
│  └─────────────────────────┬────────────────────────────────────────┘    │
│                            │                                             │
│      Long handlers run in a single-threaded background executor          │
│      (precheck, waiting, analysis). monitor + idle + holiday run inline. │
└──────────────────────────────────────────────────────────────────────────┘
                            │
                            ▼
          ┌─────────────────────────────────────────┐
          │  Per-day in-memory runtime cache         │
          │  _daily_runtime[trade_date] = {          │
          │     "paper_trader": PaperTrader,         │
          │     "monitor":      MarketMonitor,       │
          │     "plans":        [trade_plans...],    │
          │     "order_ids":    [...],               │
          │  }                                       │
          └─────────────────────────────────────────┘
                            │
                            ▼
          ┌─────────────────────────────────────────┐
          │ SQLite (cross-day persistence)           │
          │   pipeline_state / pipeline_state_history│
          │   trade_plans / agent_reports / debates  │
          │   positions                              │
          │   daily_metrics  (+ capital buckets)     │
          │   capital_log    (intraday snapshots)    │
          │   app_config / config_changes            │
          │   token_usage                            │
          └─────────────────────────────────────────┘
```

Key principles:

- **One scheduler, one job.** A single 60s APScheduler interval job
  (`dispatch_pipeline`) drives everything. There is no second cron.
- **State machine in SQLite, runtime in memory.** The `pipeline_state` table
  has exactly one row that the dispatcher reads each tick. The expensive
  per-day instances (`PaperTrader`, `MarketMonitor`) live in
  `_daily_runtime` and survive across ticks but are recreated on restart
  from DB.
- **Long handlers go to a background thread.** `precheck`, `waiting`, and
  `analysis` are offloaded to a `ThreadPoolExecutor(max_workers=1)` so the
  60s tick never blocks. `monitor`, `idle`, and `holiday` run inline.
- **Every capital change is snapshotted.** Both `daily_metrics`
  (single-row-per-day rolled state) and `capital_log` (append-only history)
  are written at each meaningful event.

---

## 2. State machine

File: `tradingagents/pipeline/state_machine.py`
File: `tradingagents/pipeline/dispatcher.py`

### 2.1 States

| State      | Meaning                                                                       | Runs in    |
| ---------- | ----------------------------------------------------------------------------- | ---------- |
| `idle`     | Waiting for `precheck_time` (default 08:10 IST).                              | inline     |
| `precheck` | Screener → multi-agent analysis. Produces actionable plans.                   | background |
| `waiting`  | Plans in hand, waiting for `execution_time` (09:30) to place orders.          | background |
| `monitor`  | Orders live. Polling prices, applying trailing stops, news exits, partials.   | inline     |
| `analysis` | Day done. Reporting + EOD reflection + capital finalize.                      | background |
| `holiday`  | NSE closed (weekend / NSE_HOLIDAYS_2026). Skips precheck until next open day. | inline     |

### 2.2 Transitions

```
idle ─[now ≥ precheck_time AND not already ran today]─► precheck
precheck ─[plans built]──────────────────────────────► waiting
waiting ─[now ≥ execution_time, orders placed]──────► monitor
monitor ─[execution window closed AND no open pos]──► analysis
analysis ─[reporting + reflection done]─────────────► idle

(any state) ─[market closed today]──► holiday ──► idle (next trading day)
```

Notes:

- `monitor` stays put past the window if open positions remain — the
  monitor's own `tick()` runs the hard-exit and the next tick promotes to
  `analysis`.
- `has_completed_today(stage, date)` is checked via
  `pipeline_state_history` so the dispatcher won't re-enter `precheck` the
  same day even if it crashes back to `idle`.

### 2.3 `pipeline_state` columns

| Column              | Purpose                                                       |
| ------------------- | ------------------------------------------------------------- |
| `state`             | One of the 6 state strings.                                   |
| `state_since`       | ISO timestamp the state was **entered**. Never bumped on heartbeat. |
| `last_heartbeat_at` | ISO timestamp updated on every no-op tick — proves "alive".   |
| `trade_date`        | Today's date in IST. Pinned by `_trade_date_for_transition`.  |
| `next_run_at`       | Informational; APScheduler does the real scheduling.          |
| `last_error`        | Traceback string when a handler crashes.                      |
| `payload`           | JSON dict per state (e.g. `{"plan_count": 3, ...}`).          |

The split between `state_since` (entry time) and `last_heartbeat_at`
(liveness) matters: it lets the UI show *both* "in state X for 22 min" and
"last alive 18s ago".

### 2.4 Dispatcher tick anatomy

`dispatch_pipeline()` runs on a 60s interval:

```
read pipeline_state from DB
if market is closed and state ∉ {analysis, holiday}:
    cancel any running bg task; transition → holiday
    return

handler = STATE_HANDLERS[state]

if state ∈ {precheck, waiting, analysis}:
    if bg_future is running:
        update heartbeat; return                    ← 60s no-op tick
    if bg_future just completed:
        next_state = bg_future.result()
        transition_to(next_state) (with trade_date)
        return
    else:
        spawn handler in bg_executor; return
else:                                               ← idle / monitor / holiday
    inline run handler
    transition_to(next_state)  OR  touch_heartbeat()
```

The key guarantee: **only one background handler can run at a time**.
`_cancel_background()` returns `False` if the bg task has already started
(Python can't safely stop a running thread), and the API layer translates
that into HTTP 409 for `force-rerun` / manual transitions to prevent racy
data writes.

---

## 3. Per-state behaviour

### 3.1 `idle` (inline)

```python
if now < precheck_time: stay
if precheck already ran today: stay
return STATE_PRECHECK
```

### 3.2 `precheck` (background, one-shot)

1. Clear today's runtime cache.
2. Screener (`run_screener(top_n=cfg.top_k_positions)`)
   - Skipped in dry-run mode (one hard-coded ticker is used).
3. Multi-agent analysis (`run_analysis_phase`) for each top stock in
   parallel — each ticker gets its own `TradingAgentsGraph` thread.
4. Cross-stock allocator (`rank_and_allocate`) picks the top-K and sizes
   them; force-promotes the best `Skip` if everything came back `Skip`.
5. Plans written to `trade_plans` + agent reports + debates.
6. Plans cached at `_daily_runtime[trade_date]["plans"]`.

Returns `STATE_WAITING` (or `STATE_IDLE` if nothing actionable).

### 3.3 `waiting` (background)

Fires once `now ≥ execution_time`. It is the **capital opening ritual**:

1. Recover plans:
   - First, in-memory cache.
   - Fallback: re-read Buy plans from `trade_plans` (handles container
     restarts mid-day).
2. Compute `starting_capital = get_latest_capital(default=initial_capital,
   before_date=trade_date)` — the EOD `capital` from the most recent
   finalized `daily_metrics` row, otherwise the configured seed.
3. Build a fresh `PaperTrader(initial_capital=starting_capital)` and cache
   it in `_daily_runtime`.
4. `capital_service.init_day(trade_date, starting_capital)`
   — seeds `daily_metrics` with `start_capital`, `free_cash = start_capital`,
   `invested = 0`, `pending_reserved = 0`, `daily_pnl = 0`, `is_finalized = 0`.
5. `_snapshot_capital(..., trigger="day_init")` writes the very first
   `capital_log` row.
6. `run_execution_phase(plans, paper_trader)` for each plan:
   - Live-price fetch + optional zone shift (see §4).
   - `paper_trader.place_trade_plan(plan)` (see §5 for guards).
7. `_snapshot_capital(..., trigger="orders_placed")`.

Returns `STATE_MONITOR` if any orders went pending, else `STATE_ANALYSIS`.

### 3.4 `monitor` (inline, throttled)

The most active state. See §6 for the full monitor loop.

### 3.5 `analysis` (background, one-shot)

1. `run_reporting_phase` — writes `daily_metrics` stats (trades, win rate,
   drawdown). Uses UPSERT so it preserves the capital buckets written by
   `waiting`/`monitor`.
2. `run_eod_reflection` — fast classifier post-mortem per closed trade,
   appended to the memory log for tomorrow's PM.
3. `_snapshot_capital(..., trigger="day_finalized")`.
4. `capital_service.finalize_day(trade_date)` —
   `capital = start_capital + daily_pnl`, `is_finalized = 1`.
   That `capital` value becomes tomorrow's `start_capital`.
5. Clear runtime cache.

Returns `STATE_IDLE`.

### 3.6 `holiday`

If the market is still closed → stay. Otherwise → `idle`.

---

## 4. Live-price zone shift (execution-time anchoring)

`run_pipeline._adjust_plan_to_live_price` runs once per ticker right before
`place_trade_plan`. Behaviour depends on `use_upper_band_only`:

- **`use_upper_band_only = True` (default).** Anchor on `entry_zone_high`.
  If `|live_price - entry_zone_high| > 1%`, shift all of
  `{entry_zone_low, entry_zone_high, stop_loss, target_1, target_2}` by
  `live_price - entry_zone_high`, preserving R:R. Order behaves like a
  buy-limit at the upper band.
- **`use_upper_band_only = False` (legacy).** Anchor on zone mid; expand
  the zone around `live_price` keeping the half-width.

If the live price is within the tolerance, the agent's levels are used
verbatim. Adjusted levels are persisted to `trade_plans.price_adjusted_pct`
so the UI can show "(zone shifted +1.3%)".

---

## 5. Order placement

`PaperTrader.place_trade_plan(plan)` — every check below has to pass or the
order is rejected:

1. **Pause check.** `trading_paused` is set when the daily loss limit hits;
   no new orders until reset.
2. **Duplicate guard.** Reject if any order for that ticker is `PENDING` or
   `FILLED` today, or a position is already open.
3. **Plan completeness.** When `use_upper_band_only` is true, require
   `entry_zone_high`, `stop_loss`, `target_1` (and default `entry_zone_low`
   to `entry_zone_high` if missing). Otherwise require both bands.
4. **Capital availability.**
   - `pending_reserved = Σ entry_zone_high × qty` over **other** pending
     orders.
   - `available = position_tracker.capital − pending_reserved`.
   - `max_capital = available × min(position_size_pct,
     max_capital_per_stock_pct) / 100`.
5. **Quantity sizing.**
   - `risk_per_share = entry_high − stop_loss` (must be > 0).
   - `max_loss_inr = available × max_loss_per_trade_pct / 100`.
   - `qty = floor(max_loss_inr / risk_per_share)`.
   - Capped by `max_capital`: `qty = floor(max_capital / entry_high)`.
6. **Hard checks.**
   - `qty > 0` else "insufficient free cash".
   - `capital_needed = qty × entry_high ≤ available`.
   - `pending_reserved + invested + capital_needed ≤ initial_capital`
     ("total commitments ceiling") — catches partial-fill / rounding drift.

When all pass, the order goes into `OrderManager` with status `PENDING`.

> **Order ID is shared with the eventual position** — `OrderManager`'s
> `order_id` is reused as `Position.order_id` so `check_exit` on every tick
> can look up the correct SL/target levels even after trailing.

---

## 6. Monitoring pipeline (the live loop)

This is where the project spends most of the day. `handle_monitor` in
`dispatcher.py` runs **inline** on the 60s dispatcher tick, but the real
poll work is throttled to `dispatcher_monitor_interval_sec`
(default 600 = 10 minutes).

### 6.1 Tick entry — `handle_monitor`

```
poll_interval = 60 if dry_run else dispatcher_monitor_interval_sec
hard_exit_due = now ≥ hard_exit_time  (default 15:15)

# Gates
if not hard_exit_due and now < execution_window_start (10:30): return
if not hard_exit_due and elapsed_since_last_tick < poll_interval: return

# Recover the day's PaperTrader from cache or DB
paper_trader = _daily_runtime[date]["paper_trader"]
           or _restore_paper_trader_from_db(date, cfg)

# Nothing to do? Jump straight to analysis.
if no pending orders and no open positions: return STATE_ANALYSIS

# Build/reuse the MarketMonitor (with risk thresholds + news monitor)
monitor = _daily_runtime[date]["monitor"]  # reused across ticks

window_closed = monitor.tick(now)          # see §6.2
_snapshot_capital(date, paper_trader,
                  trigger="hard_exit" if window_closed else "monitor_tick",
                  current_prices=monitor._last_prices)
```

`_restore_paper_trader_from_db` is the recovery path for process restarts:
- Rebuilds open positions from the `positions` table (status=open today).
- Re-places pending orders from `trade_plans` for any Buy with no
  matching position yet.

The `MarketMonitor` instance is **deliberately reused** across ticks so
internal state like the dry-run price index and `_last_prices` survives.

### 6.2 What `MarketMonitor.tick()` does

`tradingagents/pipeline/market_monitor.py`

```
1. _reload_config()         ← pulls live values from app_config
                              (risk thresholds, news toggle,
                               execution window, dry-run flag)

2. if (not dry_run) and outside execution window:
       _hard_exit_all(now)
       return True                ← window closed; tell dispatcher

3. tickers = open orders ∪ open positions
   prices  = yfinance fast_info (or dry-run sequence)
   self._last_prices = {valid subset}

4. apply_trailing_stops(positions, all_orders, prices)
       - Breakeven trigger: if unrealized% ≥ breakeven_trigger_pct,
         raise SL to entry.
       - Trail trigger:     if unrealized% ≥ trail_trigger_pct,
         raise SL to lock trail_lock_pct.
       - SL only ratchets up; never lowered.
       - Both Position.stop_loss AND Order.stop_loss are mutated so
         check_exit on the same tick sees the new value.

5. _evaluate_news(now, prices)
       For each open position:
         classifier reads recent headlines (lookback_min minutes).
         On EXIT decision (halt / fraud / downgrade / regulator):
             force_exit_position(ticker, current_price, "news_exit")

6. for ticker, price in prices:
       events = paper_trader.on_price_tick(ticker, price, now)
       for event in events: _handle_event(event, now)
                                                   ↓
                          inserts/updates the positions table
                          ("entry", "partial_exit", "exit")

7. log "Poll at HH:MM:SS: N tickers, free_cash=X, open=K"
8. return False              ← window still open
```

### 6.3 `paper_trader.on_price_tick`

For each price tick:

```
1. Daily loss check
   if daily_loss >= daily_loss_limit_pct × initial_capital:
       trading_paused = True; bail.

2. Hard exit time (15:15) → close any open position for that ticker.

3. Pending orders for ticker
   check_entry(price) →
       if entry_zone_low ≤ price ≤ entry_zone_high:
           fill order, create Position(entry_price=filled_price),
           position_tracker.add_position(pos, capital_used)
       (`add_position` deducts capital_used from free cash.)

4. Open positions for ticker
   check_exit(order_id, price) returns one of:
     "sl"      → close_position("stop_loss")
     "target1" →
        if qty < 2: close_position("target_1")
        else: partial_close_position(qty // 2, "target_1")
              (book half; remainder rides for T2)
     "target2" → close_position("target_2")

5. Return [events]
```

### 6.4 Capital snapshot on every tick

Right after `monitor.tick()` returns, the dispatcher calls
`_snapshot_capital(...)` which does **two writes**:

1. `daily_metrics` UPDATE (single rolled row per day):
   - `free_cash`, `invested`, `pending_reserved`, `daily_pnl`.
2. `capital_log` INSERT (append-only history):
   - `at`, `start_capital`, `current_value`, `free_cash`, `invested`,
     `pending_reserved`, `realized_pnl`, `unrealized_pnl`,
     `open_positions_count`, `trigger`.

Triggers in use today:

| Trigger         | Emitted by                                        |
| --------------- | ------------------------------------------------- |
| `day_init`      | `handle_waiting` after `capital_service.init_day` |
| `orders_placed` | `handle_waiting` after `run_execution_phase`      |
| `monitor_tick`  | `handle_monitor` after each `monitor.tick()`      |
| `hard_exit`     | `handle_monitor` when `tick()` returned True      |
| `day_finalized` | `handle_analysis` after EOD reflection            |

---

## 7. Capital model

File: `tradingagents/web/capital_service.py`

```
seed_capital      = DEFAULT_CONFIG["initial_capital"]   (constant lifetime)
start_capital     = yesterday's EOD daily_metrics.capital
                    (= seed_capital on day 1)
free_cash         = cash available to place new orders
                    = position_tracker.capital − pending_reserved
invested          = Σ entry_price × qty   over open positions
pending_reserved  = Σ entry_zone_high × qty   over PENDING orders
realized_pnl      = position_tracker.daily_pnl  (cumulative for the day)
unrealized_pnl    = Σ (current_price − entry_price) × qty  (open positions)

current_value     = start_capital + realized_pnl              (live)
mtm_value         = current_value + unrealized_pnl
```

Invariant (modulo rounding & charges):

```
free_cash + invested + pending_reserved == start_capital + realized_pnl
```

Lifecycle within a single trading day:

| Time          | Event                | Capital effect                          |
| ------------- | -------------------- | --------------------------------------- |
| 09:30         | `init_day`           | `start_capital := free_cash := seed/prev EOD` |
| 09:30         | order placed         | `pending_reserved += entry_high × qty`  |
| 10:30–15:15   | fill                 | `pending_reserved -= …`; `invested += …`; `free_cash -= filled × qty` |
| 10:30–15:15   | exit (full/partial)  | `invested -= …`; `free_cash += proceeds`; `realized_pnl += pnl` |
| 15:15         | hard exit            | all opens closed                        |
| 15:20         | `finalize_day`       | `capital := start_capital + realized_pnl`; `is_finalized := 1` |

> Tomorrow morning, `get_latest_capital(before_date=today)` reads
> yesterday's finalized `capital` — that's tomorrow's `start_capital`.
> If yesterday wasn't finalized (crash, weekend), the most recent prior
> finalized row is used; first-ever run falls back to `initial_capital`.

---

### 7.1 `positions` row model — one row per closed slice

Every fill writes exactly one open row to `positions`. Each exit
materialises the **closed slice** as its own row:

| Lifecycle                          | Rows in `positions` for that ticker+date                                                 |
| ---------------------------------- | ----------------------------------------------------------------------------------------- |
| Entry only (still open)            | 1 open row (qty=N).                                                                       |
| Entry → full SL/T2/hard exit       | 1 closed row (qty=N, reason=…).                                                           |
| Entry → T1 partial → full exit     | 2 closed rows: (qty=N/2, reason=target_1) + (qty=N/2, reason=…). Sum of `pnl` == `daily_metrics.daily_pnl` for that ticker (modulo rounding). |

`update_position_partial_exit` keeps the **remaining open row** alive
with the reduced qty + raised SL while the new partial-slice row is
inserted as closed. `update_position_exit` later flips that remaining
row to closed when the runner exits.

This means `SUM(pnl) FROM positions WHERE date=… AND status='closed'`
always reconciles with `daily_metrics.daily_pnl` for that date.

---

## 8. Database schema (what each table is for)

| Table                     | Cardinality          | Role                                         |
| ------------------------- | -------------------- | -------------------------------------------- |
| `pipeline_state`          | 1 row                | Current state of the daily cycle.            |
| `pipeline_state_history`  | 1 row per transition | Append-only audit of every state change.     |
| `trade_plans`             | 1 row per ticker/day | The PM's plan — entry/SL/target/confidence.  |
| `agent_reports`           | many per ticker/day  | Raw analyst markdown reports.                |
| `debates`                 | 1 row per ticker/day | Bull/bear/judge transcript.                  |
| `positions`               | 1 row per fill       | Open + closed positions (lifecycle here).    |
| `daily_metrics`           | 1 row per day        | EOD stats + intraday capital buckets.        |
| `capital_log`             | many per day         | Append-only intraday capital history.        |
| `app_config`              | ~50 rows             | DB-backed runtime config (replaces dict).    |
| `config_changes`          | append               | Audit trail of config PATCHes.               |
| `token_usage`             | per stage            | LLM call accounting.                         |
| `on_demand_analyses`      | per request          | UI-triggered ad-hoc analyses.                |

Key `daily_metrics` columns added for the capital model:
`start_capital`, `free_cash`, `invested`, `pending_reserved`, `is_finalized`.
`insert_daily_metrics` is an UPSERT that only overwrites the stats columns
so the capital buckets (set by `capital_service`) are preserved.

---

## 9. HTTP API surface (the contract the UI uses)

Backend: `tradingagents/web/routes/*.py` → mounted under `/api`.

### Dashboard / capital
- `GET  /api/today?date=YYYY-MM-DD` — full day-view payload: pipeline state,
  plans, positions, today's `portfolio` (live capital state), token stats.
- `GET  /api/global-summary` — multi-day capital/return chart data.
- `GET  /api/capital/log?date=&limit=` — intraday capital history (newest
  first) for the on-screen log table.

### Pipeline control
- `GET  /api/pipeline/state` — state + state_since + last_heartbeat_at.
- `POST /api/pipeline/transition` — manual override
  (409 if a bg task is running).
- `POST /api/pipeline/run-now/{stage}` — force a state immediately
  (409 if a bg task is running).
- `POST /api/pipeline/force-rerun` — cancel pending bg task, delete today's
  trade rows, re-enter precheck. Hard-stops the deletion when a bg task is
  already running.

### Config
- `GET   /api/config`, `PATCH /api/config/{key}`, `GET /api/config/history`
  — runtime config CRUD against `app_config`.

### Trades / positions / debates / files / etc.
Standard CRUD over the matching tables (see `routes/`).

---

## 10. UI surfaces

Frontend: `frontend/src/pages/Today.jsx` is the main control panel.

```
┌────────────────────────────────────────────────────────────────────┐
│  Pipeline state badge   (state · since hh:mm · live hh:mm)         │
│  Action buttons         (Run now, Force rerun, transition)         │
├────────────────────────────────────────────────────────────────────┤
│  Capital tiles                                                     │
│    Invested Amount (seed) │ Current Value │ Free Cash │ Realized P&L│
├────────────────────────────────────────────────────────────────────┤
│  Trade plans for today   (entry zone, SL, T1/T2, confidence)       │
├────────────────────────────────────────────────────────────────────┤
│  Open positions          (live SL after trailing, P&L)             │
├────────────────────────────────────────────────────────────────────┤
│  Capital log             (per monitor check; new component)        │
│    Time · Trigger · Current · Free Cash · Invested · Pending ·     │
│    Realized · Unrealized · Open                                    │
├────────────────────────────────────────────────────────────────────┤
│  Live debate stream / token usage / day files                      │
└────────────────────────────────────────────────────────────────────┘
```

Polling intervals:
- `/api/today` every **5s**.
- `/api/capital/log` every **5s** (via `CapitalLogTable`).
- `/api/tokens` every **10s**.
- `/api/files` every **15s**.

The capital log is **append-only**, newest first, limited to 200 rows by
default (≈40 entries on a normal trading day, plus the `day_init` /
`orders_placed` / `day_finalized` events).

Settings page (`Settings.jsx` + `SettingsForm.jsx`) writes through
`/api/config/{key}`; the monitor and dispatcher pick up changes on the
next tick (no restart needed).

---

## 11. Configuration keys you'll actually touch

| Key                              | Default  | Effect                                                |
| -------------------------------- | -------- | ----------------------------------------------------- |
| `initial_capital`                | 20000    | First-day seed. Ignored once `daily_metrics` exists.  |
| `min_capital_to_trade`           | 5000     | Floor; below this the day is skipped.                 |
| `use_upper_band_only`            | true     | Anchor zone shift + order on `entry_zone_high`.       |
| `top_k_positions`                | 3        | How many stocks the allocator promotes to trades.     |
| `deploy_pct_top_k`               | 70       | % of capital to deploy across the top-K.              |
| `max_capital_per_stock_pct`      | 25       | Hard per-stock cap.                                   |
| `max_loss_per_trade_pct`         | 1.5      | Sizes qty so a stop-out loses at most this %.         |
| `precheck_time`                  | 08:10    | IST. Idle → precheck.                                 |
| `execution_time`                 | 09:30    | IST. Waiting → monitor (order placement).             |
| `execution_window_start`         | 10:30    | Monitor starts polling at this time.                  |
| `execution_window_end`           | 15:15    | Monitor's window-closed threshold.                    |
| `hard_exit_time`                 | 15:15    | Force-close everything.                               |
| `dispatcher_monitor_interval_sec`| 600      | Throttle `monitor.tick()` to once per N seconds.      |
| `breakeven_trigger_pct`          | 0.5      | Unrealized% that raises SL to entry.                  |
| `trail_trigger_pct`              | 1.0      | Unrealized% that activates the trail.                 |
| `trail_lock_pct`                 | 0.3      | % below current price the trail locks in.             |
| `news_check_enabled`             | true     | Run the news force-exit classifier each poll.         |
| `news_check_lookback_min`        | 60       | Lookback window for fresh headlines.                  |
| `dry_run_e2e`                    | false    | Use scripted prices + skip market-hours gate.         |

---

## 12. Charges / cost model (for forward-looking realism)

P&L today is paper-only. When live trading lands, the Zerodha-style
intraday (MIS) charges that will be baked into `Position.pnl`:

- Brokerage: 0.03% or ₹20, whichever is lower, per executed side.
- STT (sell side intraday): 0.025% of sell turnover.
- Exchange transaction charges: NSE 0.00297% per side.
- GST: 18% of (brokerage + transaction).
- SEBI: ₹10 per crore (negligible).
- Stamp duty (buy side): 0.003% of buy turnover.

These are subtracted at exit so `realized_pnl` is net-of-fees by the time
it lands in `daily_metrics` / `capital_log`.

---

## 13. Failure / recovery scenarios

| Scenario                                       | What protects us                                                                                                            |
| ---------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------- |
| Process restart mid-day                        | `_restore_paper_trader_from_db` rebuilds positions + pending orders. `init_day` is idempotent and back-fills `start_capital`. |
| `precheck` crashes after some plans saved      | Plans are in `trade_plans`; `waiting`'s cache-miss path reloads them.                                                       |
| `force_rerun` while bg task running            | API returns 409. `_cancel_background()` won't delete data while a writer thread is alive.                                   |
| Same state held all day (no events)            | `last_heartbeat_at` advances every tick; `state_since` doesn't — UI shows liveness without losing "in-state-since".         |
| Yesterday wasn't finalized (crash/weekend)     | `get_latest_capital(before_date)` walks back to the most recent finalized row.                                              |
| Partial exit double-bookkeeping                | `update_position_partial_exit` mutates the existing row (qty, SL, T1, T2) without flipping `status=closed`.                 |
| Trailing stop race vs check_exit on same tick  | The risk ladder mutates both `Position.stop_loss` AND the parent `Order.stop_loss` so `check_exit` reads the raised value.  |
| Over-allocation across multiple pending orders | `_pending_reserved_capital()` is subtracted from `available` in `place_trade_plan`, plus a "total commitments ≤ initial" check. |

---

## 14. File map (where to look when something breaks)

```
run_pipeline.py                     ← phases (screener / analysis / execution / report)
tradingagents/
  pipeline/
    state_machine.py                ← StateRow, transition_to, read_state, heartbeat
    dispatcher.py                   ← APScheduler job, state handlers, capital snapshots
    market_monitor.py               ← tick(), trailing stops, news exits, hard exit
    plan_extractor.py               ← parse PM markdown → plan dict
    allocator.py                    ← rank_and_allocate top-K
    news_monitor.py                 ← classifier-driven force-exit
  execution/
    paper_trader.py                 ← place_trade_plan, on_price_tick, get_capital_state
    order_manager.py                ← Order, OrderStatus, check_entry, check_exit
    position_tracker.py             ← Position, capital, partial_close
    risk_manager.py                 ← apply_trailing_stops, RiskThresholds
  web/
    database.py                     ← schema, init_db, migrations, CRUD
    capital_service.py              ← init_day, snapshot, log_snapshot, get_log, finalize_day
    config_service.py               ← load_config / set_config against app_config
    routes/
      pipeline.py                   ← /api/pipeline/*
      dashboard.py                  ← /api/today, /api/capital/log, /api/global-summary
      config.py                     ← /api/config*
      positions.py, trades.py, …
  default_config.py                 ← seed values + metadata for app_config
frontend/
  src/
    pages/Today.jsx                 ← live control panel
    pages/Settings.jsx              ← config editor
    components/
      CapitalLogTable.jsx           ← new — per-check capital history
      PositionTableLive.jsx
      PipelineStateBadge.jsx
      SettingsForm.jsx
```

---

## 15. Day-in-the-life timeline (IST)

```
08:10  idle → precheck       (screener + LLM analysis fan-out)
08:50  precheck → waiting    (plans saved, allocator picked top-K)
09:30  waiting fires:
         - init_day(start_capital)
         - capital_log: "day_init"
         - place pending orders
         - capital_log: "orders_placed"
       waiting → monitor
10:30  monitor begins polling (every 10 min)
         every tick: trailing stops → news scan → fills/exits
                     → capital_log: "monitor_tick"
13:42  partial_exit T1 → positions table updated → capital_log row
14:55  trailing stop lifted SL to ₹X.XX (no exit)
15:15  monitor.tick() sees window closed → hard_exit_all
         capital_log: "hard_exit"
       monitor → analysis
15:16  analysis:
         - run_reporting_phase (daily_metrics UPSERT)
         - run_eod_reflection (per-trade post-mortem)
         - capital_log: "day_finalized"
         - capital_service.finalize_day (capital = start + realized)
       analysis → idle
       (next morning, start_capital = today's finalized capital)
```

---

## 16. Dry-run E2E mode

For testing the whole flow without waiting for the market or burning LLM
tokens. Gated by the existing `dry_run_e2e` flag in `app_config`
(seeded from `default_config.py`). No new flags were added.

### 16.1 What dry-run mode changes

| Surface                              | Normal mode                                      | Dry-run mode                                                       |
| ------------------------------------ | ------------------------------------------------ | ------------------------------------------------------------------ |
| `is_market_closed` gate (dispatcher) | Forces `STATE_HOLIDAY` on weekends/holidays.     | **Skipped.** Cycle runs any day.                                   |
| `precheck_time` (08:10) gate         | `idle → precheck` blocked until 08:10 IST.       | **Skipped.** `idle → precheck` on the next tick.                   |
| `has_completed_today("precheck")`    | Prevents re-entering precheck same day.          | **Skipped.** Re-runnable on demand.                                |
| Screener                             | NSE universe → top-K.                            | **Skipped.** `dry_run_ticker` is the chosen stock.                 |
| Multi-agent analysis + debate        | Full LangGraph fan-out, ~$N tokens.              | **Skipped entirely.** Mock plan built from `dry_run_plan`.         |
| `execution_time` (09:30) gate        | `waiting → monitor` blocked until 09:30 IST.     | **Skipped.** Orders placed on the next tick.                       |
| Live price fetch in execution        | `yfinance` 1m bar.                               | **Skipped.** Levels come from `dry_run_plan` config.               |
| `execution_window_start` (10:30) gate| Monitor doesn't poll before 10:30.               | **Skipped.**                                                       |
| Monitor poll throttle                | `dispatcher_monitor_interval_sec` (default 600s).| **Skipped.** Monitor runs every dispatcher tick.                   |
| Monitor price source                 | yfinance fast_info.                              | Scripted `dry_run_price_sequence`, cycles when exhausted.          |
| Window-closed gate inside `tick()`   | `is_execution_window` checked.                   | **Skipped.** (Already the existing behaviour for dry-run.)         |
| News monitor (LLM classifier)        | Polls headlines each tick.                       | **Disabled** so dry-run is fully offline.                          |
| Monitor → analysis transition        | Driven by hard-exit time + zero open work.       | One dispatcher tick burns the full price sequence, cancels unfilled pending orders, and force-exits leftover positions. Always returns `STATE_ANALYSIS`. |
| `STATE_HOLIDAY`                      | Stays until next trading day.                    | **Auto-flips back to idle** so the cycle can loop.                 |

### 16.2 One-tick-per-state cadence

With dry-run enabled, the 60s dispatcher tick advances the state machine
deterministically:

```
Tick 1   idle      → precheck    (precheck spawned in bg; ~ms)
Tick 2   precheck  → waiting     (mock plan persisted to trade_plans)
Tick 3   waiting   → monitor     (init_day, orders_placed, position_log)
Tick 4   monitor:
           - burns entire dry_run_price_sequence in one tick
           - cancels unfilled pending orders
           - force-exits leftover positions
           → analysis
Tick 5   analysis  → idle        (finalize_day; capital becomes EOD)
```

Total: **~5 ticks ≈ 5 minutes** for a full cycle, with no LLM calls and
no external API calls. The `capital_log` accumulates the usual triggers
plus one new dry-run-only trigger: `dry_run_force_exit`.

### 16.3 What's still real

Dry-run keeps the parts that *should* be exercised:

- `PaperTrader.place_trade_plan` (all capital + math guards run).
- `OrderManager.check_entry` / `check_exit` against the scripted prices.
- The trailing-stop ladder in `RiskManager`.
- `MarketMonitor._handle_event` writing to `positions` (`is_dry_run=1`).
- `capital_service.init_day / snapshot / log_snapshot / finalize_day`.
- `daily_metrics` UPSERT and `pipeline_state_history` audit rows.
- Every dashboard/capital-log endpoint and UI render path.

### 16.4 Knobs (all already in `app_config`)

| Key                       | What it controls                                                 |
| ------------------------- | ---------------------------------------------------------------- |
| `dry_run_e2e`             | The master toggle. Flip to `true` to enter dry-run mode.         |
| `dry_run_ticker`          | The single ticker the synthetic plan is built for.               |
| `dry_run_plan`            | `entry_zone_low/high`, `stop_loss`, `target_1/2`, `confidence_score`, `position_size_pct`. Levels that the monitor's scripted prices interact with. |
| `dry_run_price_sequence`  | Ordered list of prices fed to `MarketMonitor` each internal tick. Cycles when exhausted. |

### 16.5 Running it

1. Toggle `dry_run_e2e` to `true` (UI Settings page → Testing section, or
   `PATCH /api/config/dry_run_e2e`).
2. Wait for the next dispatcher tick (≤ 60s) or hit
   `POST /api/pipeline/run-now/precheck` to start immediately.
3. Watch the Capital log panel: you'll see `day_init`, `orders_placed`,
   a burst of `monitor_tick` rows, possibly a `dry_run_force_exit`, then
   `day_finalized`.
4. Flip `dry_run_e2e` back to `false` before market open to resume
   normal operation.

### 16.6 What dry-run intentionally does NOT exercise

- The LangGraph multi-agent debate (already covered by the live pipeline
  and not what this mode is testing).
- The screener / universe ranking.
- yfinance / news / FII-DII data fetches.
- The fast-classifier-driven news force-exit path.

If you change any of those subsystems, run a live precheck against a
single ticker — dry-run will silently bypass them.

---

## 17. Telegram side-channel (live event stream)

A small fire-and-forget Telegram notifier streams every meaningful pipeline
event to a chat of your choice. This is the recommended way to follow what
the system is doing day-to-day — the dashboard UI exists, but Telegram is
push, mobile-friendly, and outlives any frontend bug.

### 17.1 What gets sent

| Event                              | Fired by                                  | Body content                                                                                                           |
| ---------------------------------- | ----------------------------------------- | ---------------------------------------------------------------------------------------------------------------------- |
| Precheck started                   | `handle_precheck` top                     | `trade_date`, `mode` (live / DRY RUN).                                                                                 |
| Precheck complete                  | `handle_precheck` end                     | One line per actionable plan: ticker, rating, conf/10, entry zone, SL, T1, T2.                                         |
| Precheck: no actionable plans      | `handle_precheck` early-return            | Plain note, back to idle.                                                                                              |
| Day initialised                    | `handle_waiting` after `init_day`         | `trade_date`, `start_capital`, `plans_pending`.                                                                        |
| Order placement                    | `handle_waiting` after `run_execution_phase` | "Placed (N)" block with qty/levels per ticker + "Rejected (M)" block with the verbatim reason from `place_trade_plan`. |
| No orders placed — skipping monitor| `handle_waiting` if 0 fills              | Plain note.                                                                                                            |
| ENTRY                              | `MarketMonitor._handle_event(type=entry)` | ticker, qty, entry_price, SL, T1, T2.                                                                                  |
| PARTIAL EXIT                       | `MarketMonitor._handle_event(type=partial_exit)` | ticker, exit_price, qty, reason, pnl, pnl%, remaining qty, new SL.                                                |
| EXIT                               | `MarketMonitor._handle_event(type=exit)`  | ticker, exit_price, reason, pnl, pnl%.                                                                                 |
| Monitor tick                       | `handle_monitor` after each `monitor.tick()` | trade_date, current_value, free_cash, invested, pending, realized P&L, unrealized P&L, open positions count.        |
| Hard exit fired                    | `handle_monitor` when window closes       | Same as Monitor tick, with title flipped.                                                                              |
| Day closed                         | `handle_analysis` after `finalize_day`    | start_capital, end_capital, daily_pnl (₹ + %), trades, wins, win rate, max drawdown.                                  |
| Pipeline crash                     | dispatcher bg-handler exception path      | state, trade_date, last line of traceback.                                                                             |

### 17.2 Why-it-was-rejected reporting

`PaperTrader.place_trade_plan` now stores a verbatim reason on every
rejection in `paper_trader.last_rejection_reason[ticker]`. The dispatcher
reads that map after `run_execution_phase` and surfaces each rejection in
the Telegram order-placement message, so questions like "why was X not
ordered?" are answerable from your phone:

- `trading paused — daily loss limit hit`
- `duplicate: order already exists (status=pending)`
- `already holding an open position for this ticker`
- `incomplete plan (need entry_zone_high, stop_loss, target_1)`
- `invalid risk: SL ₹X >= entry ₹Y`
- `insufficient free cash — available ₹A (free_cash ₹B, pending_reserved ₹C)`
- `capital_needed ₹X > available ₹Y`
- `would breach capital ceiling: pending ₹A + invested ₹B + this ₹C = ₹D > initial ₹E`

### 17.3 Wiring it up

Three knobs in `app_config` (created automatically; edit via Settings page
or `PATCH /api/config/<key>`):

| Key                                 | Default | Notes                                                              |
| ----------------------------------- | ------- | ------------------------------------------------------------------ |
| `telegram_notifications_enabled`    | `false` | Master toggle. No HTTP calls happen when this is off.              |
| `telegram_bot_token`                | `""`    | From @BotFather. Marked secret in the config API.                  |
| `telegram_chat_id`                  | `""`    | Your user id, group id, or channel id (channels look like `-100…`).|

Steps:

1. Talk to [@BotFather](https://t.me/BotFather), `/newbot`, copy the token.
2. Start a chat with your bot and send any message (Telegram won't deliver
   *to* the bot until you've messaged it once).
3. Get your chat id — easiest way is `curl
   "https://api.telegram.org/bot<TOKEN>/getUpdates"` after sending a message
   — the `chat.id` field in the response is what you want.
4. PATCH the three config keys, then verify with the test endpoint:

```bash
curl -X POST http://localhost:8000/api/telegram/test | jq
curl http://localhost:8000/api/telegram/status | jq
```

### 17.4 Failure model

The whole notifier is wrapped in defensive try/except: a flapping Telegram
endpoint, an expired token, or a bad chat id will produce a single
`logger.warning` (rate-limited to once per minute for missing creds) but
will never propagate into the dispatcher. The trading pipeline is
guaranteed to keep running with or without Telegram.

HTTP sends happen on a small `ThreadPoolExecutor(max_workers=2)` so the
dispatcher's 60s tick is never blocked on Telegram latency.

### 17.5 Scheduled lifecycle notifications

In addition to the per-event stream, two scheduled notifications run
without any caller having to fire them:

#### Startup message — every reboot

Fired from `app.py`'s FastAPI `lifespan` startup, just after the cron
dispatcher is registered. Body:

- host (server hostname)
- python version
- pipeline state (idle / monitor / …)
- trade_date
- start_capital (today)
- current_value (today)
- realized_pnl (today)

Gated by `telegram_notifications_enabled` AND
`telegram_startup_message_enabled` (default `true`), so you can silence
reboot noise during a maintenance window without disabling the rest of
the notifier.

#### Morning brief — daily at the configured time

Fired from the first dispatcher tick at or after
`telegram_morning_message_time` (default `08:00` IST). Body:

- starting_capital (today's expected start — yesterday's EOD if
  finalized, otherwise live carry-forward)
- previous_day_end_capital
- previous_day_pnl
- previous_day_finalized (yes / no — carrying live value)

Idempotent across reboots: after a successful send the date is written
to `telegram_morning_message_last_date` in `app_config`. If the server
reboots later the same day, the next tick re-checks that flag and
*won't* re-send the brief — but the **startup message** still fires, so
you always know the process came back up.

Config keys for both:

| Key                                | Default | Notes                                                            |
| ---------------------------------- | ------- | ---------------------------------------------------------------- |
| `telegram_startup_message_enabled` | `true`  | Independent of the master toggle for noise control.              |
| `telegram_morning_message_enabled` | `true`  | Same.                                                            |
| `telegram_morning_message_time`    | `08:00` | IST. Fires from the first tick at or after this time each day.   |
| `telegram_morning_message_last_date` | `""`  | **Internal**, auto-managed. Stores the date the brief last fired. Don't edit. |

### 17.6 Report attachments

The pipeline writes per-ticker markdown reports to
`<reports_dir>/<DATE>/<TICKER>/complete_report.md` plus per-agent files
under `1_analysts/`, `2_research/`, `3_trading/`, `4_risk/`,
`5_portfolio/`. The Telegram notifier can push these to your channel
in two complementary ways:

| Mode             | Fires from                                          | What gets sent                                                                 | Config gate                                       |
| ---------------- | --------------------------------------------------- | ------------------------------------------------------------------------------ | ------------------------------------------------- |
| Per-ticker live  | `run_pipeline._save_to_db` right after `save_daily_analysis` writes the report tree | `complete_report.md` for that ticker, captioned `Report · TICKER · DATE`     | `telegram_reports_per_ticker` (default `true`)    |
| End-of-day zip   | `handle_analysis` after `finalize_day`              | A single `<reports_dir>/<DATE>.zip` containing the full `<DATE>/` tree.        | `telegram_reports_eod_zip` (default `true`)       |

Both gated additionally by `telegram_reports_enabled` (default `true`)
which is a master kill-switch, and of course by
`telegram_notifications_enabled` itself.

Telegram's bot upload limit is 50 MB; we cap at ~49 MB and skip with a
warning if a single file/zip exceeds it. Daily zips have been measured
at < 1 MB even with 5+ tickers analyzed, so this is comfortably under.

Config keys:

| Key                              | Default | Purpose                                                       |
| -------------------------------- | ------- | ------------------------------------------------------------- |
| `telegram_reports_enabled`       | `true`  | Master toggle for any report attachments.                     |
| `telegram_reports_per_ticker`    | `true`  | Push each ticker's `complete_report.md` as soon as it's saved.|
| `telegram_reports_eod_zip`       | `true`  | At EOD, zip the whole `<DATE>/` tree and upload as one file.  |

The zip is also written to disk at `<reports_dir>/<DATE>.zip`, so even
if Telegram is offline you'll have the archive locally.

### 17.7 Two-way bot commands

In addition to the one-way event stream, the FastAPI process runs a tiny
long-polling worker (`tradingagents/web/telegram_bot.py`) that listens
for `/command` messages from the same chat the notifier sends to. Commands
implemented today:

| Command            | What it returns                                                                                                              |
| ------------------ | ---------------------------------------------------------------------------------------------------------------------------- |
| `/status [date]`   | That day: current value, start capital, free cash, invested, pending, realized P&L. Lifetime: seed, net P&L, days traded, total trades / wins / losses / win rate. |
| `/today  [date]`   | Plans (rating + entry/SL/T1 per ticker), open positions, and closed positions with exit reason + pnl. Defaults to today.    |
| `/trades [date]`   | Every closed trade for that day: ticker, qty, entry → exit, reason, P&L, %. Sums to a daily total. Includes partial-exit slices as their own rows. |
| `/history [N]`     | Last N (default 14, max 60) days' P&L from `daily_metrics`, plus net total.                                                  |
| `/help`            | List of available commands.                                                                                                  |
| `/start`           | Alias for `/help` (Telegram sends this on first contact).                                                                    |

`date` accepts three forms: `today`, `yesterday`, or `YYYY-MM-DD`.
Missing → defaults to today.

`/status` is the one the user asked for: current capital + invested
+ lifetime P&L all in one message.

**Security model.** Only the chat id configured in
`telegram_chat_id` is honoured. Commands from any other chat or DM are
logged and silently dropped. There's no token or shared secret beyond
the bot token itself.

**Lifecycle.** The poller starts from `app.py`'s FastAPI lifespan (right
after the startup notification) and stops when the lifespan exits. While
`telegram_notifications_enabled` is `false`, the worker just sleeps in
15-second chunks — flipping the toggle on resumes polling within 15s
without a restart.

**Setup gotcha for channels.** If you want to issue `/status` *inside* a
channel (instead of a DM to the bot), open BotFather, navigate to your
bot → **Bot Settings → Group Privacy → Disable**, and add the bot as an
admin in the channel. With Group Privacy enabled (the default), Telegram
only forwards commands explicitly addressed as `/status@yourbotname` —
which still works but is annoying to type. The simpler path is to send
`/status` as a DM to the bot itself; that always works.

### 17.8 Cadence in practice

- **Live mode**: one monitor-tick notification per
  `dispatcher_monitor_interval_sec` (default 600s) → ≈6 notifications per
  trading hour for the heartbeat, plus entry/exit/partial events as they
  fire. Total typical day: 20–40 messages.
- **Dry-run mode**: a burst of `Monitor tick` messages from the inner
  loop in `_run_dry_run_monitor` (one final monitor snapshot, plus one
  per entry/exit event) — ≈8 messages per cycle. Useful for confirming
  the wiring before live trading.

---

## 18. How to verify the system in one minute

1. Open `/api/pipeline/state` — `state` and `state_since` should match the
   badge in the UI.
2. Hit `/api/today` — `portfolio` should show non-zero `free_cash` once
   `init_day` has run.
3. Hit `/api/capital/log?date=YYYY-MM-DD` — you should see a `day_init`
   row first thing in the morning, an `orders_placed` row right after
   09:30, then `monitor_tick` rows every ~10 min.
4. Open the UI's "Capital log" panel — it polls the same endpoint every
   5s and renders the same rows colour-coded by realized/unrealized P&L.
5. PATCH `dispatcher_monitor_interval_sec` to 60 via the Settings page →
   within one minute the log starts adding a fresh row each tick.

If any of those checks is off, jump to §13 first — the failure is almost
always one of those cases.
