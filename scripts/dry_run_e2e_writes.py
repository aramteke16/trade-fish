"""End-to-end DB write verification.

Flips ``dry_run_e2e=true`` in app_config, runs ``dispatch_pipeline()`` tick
by tick until the cycle completes, then dumps every row written to every
trading-related table and cross-checks them against the public API.

Run locally:
    source .venv/bin/activate
    python scripts/dry_run_e2e_writes.py

The script is destructive *only* for today's rows:
  - Today's trade_plans / positions / daily_metrics / capital_log are
    cleared at the start so the run is reproducible.
  - app_config.dry_run_e2e is set to True and restored to its prior value
    on exit.
  - pipeline_state is reset to idle at start.
"""
from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime

# Make ``run_pipeline`` importable from background handlers spawned by the
# dispatcher. The script lives under scripts/, so sys.path[0] is scripts/ —
# without this insert, ``from run_pipeline import ...`` would fail and the
# `waiting` handler would crash in the bg thread.
_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

GREEN = "\033[92m"
RED = "\033[91m"
YEL = "\033[93m"
CYA = "\033[96m"
DIM = "\033[2m"
END = "\033[0m"


def hdr(title: str) -> None:
    print()
    print(f"{CYA}━━━ {title} ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━{END}")


def ok(m: str) -> None:
    print(f"{GREEN}✓{END} {m}")


def bad(m: str) -> None:
    print(f"{RED}✗{END} {m}")


def warn(m: str) -> None:
    print(f"{YEL}!{END} {m}")


def dim(m: str) -> None:
    print(f"{DIM}{m}{END}")


def dump_table(conn, sql: str, params: tuple = (), title: str = "") -> list[dict]:
    if title:
        print(f"\n{DIM}-- {title}{END}")
    rows = [dict(r) for r in conn.execute(sql, params).fetchall()]
    if not rows:
        dim("   (empty)")
        return rows
    keys = list(rows[0].keys())
    widths = {k: max(len(k), *(len(str(r.get(k))) for r in rows)) for k in keys}
    head = "  ".join(f"{k:<{widths[k]}}" for k in keys)
    print(f"{DIM}   {head}{END}")
    for r in rows:
        line = "  ".join(f"{str(r.get(k)):<{widths[k]}}" for k in keys)
        print(f"   {line}")
    return rows


def main() -> int:
    from tradingagents.dataflows.indian_market import IST
    from tradingagents.web.database import init_db, get_conn
    from tradingagents.web.config_service import load_config, set_config
    from tradingagents.pipeline import state_machine as sm
    from tradingagents.pipeline import dispatcher

    init_db()
    today = datetime.now(IST).strftime("%Y-%m-%d")

    hdr(f"Setup: clearing today's data ({today}) and flipping dry_run_e2e=on")
    cfg_before = load_config()
    prior_dry_run = bool(cfg_before.get("dry_run_e2e", False))
    set_config("dry_run_e2e", True)
    ok(f"app_config.dry_run_e2e: {prior_dry_run} → True")

    conn = get_conn()
    try:
        conn.execute("DELETE FROM trade_plans WHERE date = ?", (today,))
        conn.execute("DELETE FROM positions WHERE date = ?", (today,))
        conn.execute("DELETE FROM daily_metrics WHERE date = ?", (today,))
        conn.execute("DELETE FROM capital_log WHERE date = ?", (today,))
        conn.commit()
        ok("cleared trade_plans / positions / daily_metrics / capital_log")
    finally:
        conn.close()

    sm.transition_to(sm.STATE_IDLE, trade_date=None, note="dry_run_e2e_writes: reset")
    # Clear history AFTER the reset transition — the reset itself can write
    # a `<prev_state> → idle` row that would falsely trip
    # has_completed_today() on the next handle_idle call.
    conn = get_conn()
    try:
        conn.execute("DELETE FROM pipeline_state_history WHERE date(at) = ?", (today,))
        conn.commit()
    finally:
        conn.close()
    ok("pipeline_state reset to idle (and today's history wiped)")

    hdr("Driving dispatcher ticks (one per state transition expected)")
    dispatcher._clear_runtime()
    dispatcher._clear_background()

    state_seq = []
    last_state = None
    for i in range(20):
        try:
            dispatcher.dispatch_pipeline()
        except Exception as e:
            bad(f"tick {i} crashed: {e}")
            break

        # If a long-running handler was just spawned, drain it before reading.
        if dispatcher.has_active_background():
            deadline = time.time() + 60
            while dispatcher.has_active_background() and time.time() < deadline:
                time.sleep(0.25)
            # One more inline tick to harvest the bg result + transition.
            dispatcher.dispatch_pipeline()

        s = sm.read_state()
        marker = "  *" if s.state != last_state else ""
        print(f"   tick {i:2d}: state={s.state:<10} trade_date={s.trade_date}{marker}")
        if s.state != last_state:
            state_seq.append(s.state)
            last_state = s.state

        if s.state == sm.STATE_IDLE and "analysis" in state_seq:
            ok(f"cycle complete in {i+1} tick(s)")
            break

    ok(f"state sequence observed: {' → '.join(state_seq)}")

    expected = {"idle", "precheck", "waiting", "monitor", "analysis"}
    seen = set(state_seq)
    if expected.issubset(seen):
        ok(f"all expected states seen: {sorted(expected)}")
    else:
        bad(f"missing states: {sorted(expected - seen)}")

    # ------------------------------------------------------------------ DB rows
    hdr("DB writes — what landed on disk")
    conn = get_conn()
    try:
        dump_table(
            conn,
            "SELECT date, ticker, rating, entry_zone_low, entry_zone_high, "
            "stop_loss, target_1, target_2, confidence_score, position_size_pct, "
            "is_dry_run FROM trade_plans WHERE date = ? ORDER BY created_at",
            (today,),
            title="trade_plans",
        )
        dump_table(
            conn,
            "SELECT date, ticker, quantity, entry_price, exit_price, status, "
            "exit_reason, pnl, pnl_pct, is_dry_run FROM positions "
            "WHERE date = ? ORDER BY opened_at",
            (today,),
            title="positions",
        )
        dump_table(
            conn,
            "SELECT date, capital, start_capital, free_cash, invested, "
            "pending_reserved, daily_pnl, is_finalized FROM daily_metrics "
            "WHERE date = ?",
            (today,),
            title="daily_metrics",
        )
        dump_table(
            conn,
            "SELECT at, trigger, current_value, start_capital, free_cash, "
            "invested, pending_reserved, realized_pnl, unrealized_pnl, "
            "open_positions_count FROM capital_log WHERE date = ? "
            "ORDER BY id",
            (today,),
            title="capital_log",
        )
        dump_table(
            conn,
            "SELECT from_state, to_state, at, note FROM pipeline_state_history "
            "WHERE date(at) = ? ORDER BY id",
            (today,),
            title="pipeline_state_history (today's transitions)",
        )
    finally:
        conn.close()

    # ------------------------------------------------------------------ Capital invariant
    hdr("Capital invariant check (free_cash + invested + pending ≈ start + pnl)")
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT * FROM daily_metrics WHERE date = ?", (today,)
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        bad("no daily_metrics row — pipeline never reached `waiting`")
    else:
        d = dict(row)
        lhs = (d.get("free_cash") or 0) + (d.get("invested") or 0) + (d.get("pending_reserved") or 0)
        rhs = (d.get("start_capital") or 0) + (d.get("daily_pnl") or 0)
        delta = round(lhs - rhs, 2)
        print(f"   free_cash + invested + pending = {lhs:,.2f}")
        print(f"   start_capital + daily_pnl      = {rhs:,.2f}")
        if abs(delta) < 0.5:
            ok(f"invariant holds (delta = ₹{delta:.2f}, within rounding)")
        else:
            bad(f"INVARIANT VIOLATED — delta = ₹{delta:.2f}")
        if d.get("is_finalized"):
            expected_capital = (d.get("start_capital") or 0) + (d.get("daily_pnl") or 0)
            if abs((d.get("capital") or 0) - expected_capital) < 0.5:
                ok(f"capital column = start + pnl = ₹{expected_capital:,.2f}")
            else:
                bad(f"capital column drift: got ₹{d.get('capital')} expected ₹{expected_capital}")

    # ------------------------------------------------------------------ API parity
    hdr("API parity — does /api/* expose what's on disk?")
    from fastapi.testclient import TestClient
    from tradingagents.web.app import create_app

    app = create_app()
    with TestClient(app) as client:
        r = client.get(f"/api/today?date={today}")
        body = r.json() if r.status_code == 200 else {}
        ok(f"GET /api/today?date={today} → {r.status_code}")
        if body:
            print(f"   date          = {body.get('date')}")
            print(f"   plans         = {len(body.get('trade_plans', []))}")
            print(f"   open_pos      = {len(body.get('open_positions', []))}")
            p = body.get("portfolio", {})
            print(f"   portfolio:")
            for k in ("seed_capital", "start_capital", "current_value",
                      "free_cash", "invested", "pending_reserved",
                      "realized_pnl", "is_finalized", "source"):
                print(f"      {k:<20} = {p.get(k)}")

        r = client.get(f"/api/capital/log?date={today}&limit=50")
        rows = r.json().get("rows", []) if r.status_code == 200 else []
        ok(f"GET /api/capital/log?date={today} → {r.status_code}, {len(rows)} rows")
        for row in rows[-5:]:
            print(f"   {row.get('at')}  trig={row.get('trigger'):<22}  "
                  f"cur={row.get('current_value')}  "
                  f"realized={row.get('realized_pnl')}  "
                  f"open={row.get('open_positions_count')}")

        r = client.get("/api/pipeline/state")
        ps = r.json() if r.status_code == 200 else {}
        ok(f"GET /api/pipeline/state → {r.status_code}, state={ps.get('state')}, "
           f"trade_date={ps.get('trade_date')}")

        r = client.get(f"/api/positions?status=closed")
        body = r.json() if r.status_code == 200 else None
        positions = body if isinstance(body, list) else (body or {}).get("positions") or []
        today_closed = [p for p in positions if isinstance(p, dict) and p.get("date") == today]
        ok(f"GET /api/positions?status=closed → {r.status_code}, "
           f"{len(today_closed)} closed today (of {len(positions)} total returned)")

        r = client.get("/api/global-summary")
        gs = r.json() if r.status_code == 200 else {}
        ok(f"GET /api/global-summary → {r.status_code}, "
           f"current_capital={gs.get('current_capital')}, "
           f"days_traded={gs.get('days_traded')}")

    # ------------------------------------------------------------------ Restore config
    set_config("dry_run_e2e", prior_dry_run)
    hdr("Cleanup")
    ok(f"dry_run_e2e restored to {prior_dry_run}")

    hdr("Done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
