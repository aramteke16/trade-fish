"""End-to-end backend sanity check.

Runs ON THE SERVER (no HTTP, no proxy involved). Verifies that every
date-anchored piece of the pipeline agrees on today's IST date and that
the API endpoints return live data for it.

Usage:
    source .venv/bin/activate
    python scripts/sanity_e2e.py            # read-only report
    python scripts/sanity_e2e.py --repair   # if state is stuck, reset
                                             # pipeline_state to idle

Exit code is 0 on a fully healthy report, 1 if anything looks off.
"""

from __future__ import annotations

import argparse
import json
import sys
import traceback
from datetime import datetime

GREEN = "\033[92m"
RED = "\033[91m"
YEL = "\033[93m"
DIM = "\033[2m"
END = "\033[0m"


def ok(msg: str) -> None:
    print(f"{GREEN}✓{END} {msg}")


def bad(msg: str) -> None:
    print(f"{RED}✗{END} {msg}")


def warn(msg: str) -> None:
    print(f"{YEL}!{END} {msg}")


def section(title: str) -> None:
    print()
    print(f"{DIM}─── {title} ───────────────────────────────────────────{END}")


_failures: list[str] = []


def fail(label: str) -> None:
    _failures.append(label)


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--repair", action="store_true",
                   help="Reset stuck pipeline_state to idle for today's IST date.")
    args = p.parse_args()

    # ------------------------------------------------------------------ env
    section("Environment / IST")
    try:
        from tradingagents.dataflows.indian_market import IST
        ist_now = datetime.now(IST)
        ist_today = ist_now.strftime("%Y-%m-%d")
        server_now = datetime.now()
        ok(f"server local time = {server_now.strftime('%Y-%m-%d %H:%M:%S')} "
           f"(tz unaware)")
        ok(f"IST now            = {ist_now.strftime('%Y-%m-%d %H:%M:%S %Z')}")
        ok(f"IST today          = {ist_today}")
        if server_now.strftime("%Y-%m-%d") != ist_today:
            warn("server-local date differs from IST date — this is the "
                 "exact case the IST fix targets")
    except Exception as e:
        bad(f"IST import failed: {e}")
        fail("IST")
        return 1

    # ------------------------------------------------------------------ db
    section("Database integrity")
    try:
        from tradingagents.web.database import get_conn, init_db
        init_db()
        conn = get_conn()
        try:
            tables = {r["name"] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()}
            required = {
                "pipeline_state", "pipeline_state_history",
                "trade_plans", "positions", "daily_metrics", "capital_log",
                "app_config",
            }
            missing = required - tables
            if missing:
                bad(f"missing tables: {sorted(missing)}")
                fail("schema")
            else:
                ok(f"schema has all required tables ({len(required)})")

            dm_cols = {row[1] for row in conn.execute(
                "PRAGMA table_info(daily_metrics)"
            ).fetchall()}
            for c in ("start_capital", "free_cash", "invested",
                      "pending_reserved", "is_finalized"):
                if c in dm_cols:
                    ok(f"daily_metrics has column `{c}`")
                else:
                    bad(f"daily_metrics MISSING column `{c}`")
                    fail(f"daily_metrics.{c}")

            cl_cols = {row[1] for row in conn.execute(
                "PRAGMA table_info(capital_log)"
            ).fetchall()}
            for c in ("start_capital", "current_value", "unrealized_pnl",
                      "trigger"):
                if c in cl_cols:
                    ok(f"capital_log has column `{c}`")
                else:
                    bad(f"capital_log MISSING column `{c}`")
                    fail(f"capital_log.{c}")
        finally:
            conn.close()
    except Exception:
        bad(f"DB inspection failed:\n{traceback.format_exc()}")
        fail("db")

    # ------------------------------------------------------------------ default-date helpers all in agreement
    section("Default-date helpers (must all equal IST today)")
    try:
        from tradingagents.web.database import _today_ist as db_today
        from tradingagents.web.capital_service import _today_ist as cs_today
        from tradingagents.web.routes.dashboard import _today_ist as rt_today

        for name, fn in (("database._today_ist", db_today),
                         ("capital_service._today_ist", cs_today),
                         ("routes.dashboard._today_ist", rt_today)):
            v = fn()
            if v == ist_today:
                ok(f"{name}() = {v}")
            else:
                bad(f"{name}() = {v}  (expected {ist_today})")
                fail(name)
    except Exception:
        bad(f"helper import failed:\n{traceback.format_exc()}")
        fail("today_ist helpers")

    # ------------------------------------------------------------------ pipeline state
    section("Pipeline state")
    try:
        from tradingagents.pipeline import state_machine as sm
        row = sm.read_state()
        ok(f"state             = {row.state}")
        ok(f"state_since       = {row.state_since}")
        ok(f"last_heartbeat_at = {row.last_heartbeat_at}")
        ok(f"trade_date        = {row.trade_date}")
        if row.trade_date and row.trade_date != ist_today and row.state != "idle":
            warn(f"trade_date is {row.trade_date} but IST today is {ist_today} "
                 f"— state machine thinks it's still yesterday. "
                 f"Re-run with --repair to reset.")
            if args.repair:
                sm.transition_to(
                    sm.STATE_IDLE,
                    trade_date=None,
                    note="sanity_e2e --repair: reset stale trade_date",
                )
                ok(f"repaired: state reset to idle (was {row.state})")
    except Exception:
        bad(f"state_machine read failed:\n{traceback.format_exc()}")
        fail("state_machine")

    # ------------------------------------------------------------------ today's rows
    section(f"Today's DB rows for {ist_today}")
    try:
        from tradingagents.web.database import get_conn

        conn = get_conn()
        try:
            plans = conn.execute(
                "SELECT id, ticker, rating, entry_zone_high, stop_loss, target_1, "
                "is_dry_run, created_at FROM trade_plans WHERE date = ? "
                "ORDER BY created_at",
                (ist_today,),
            ).fetchall()
            ok(f"trade_plans rows  = {len(plans)}")
            for p in plans:
                print(f"      • #{p['id']} {p['ticker']:<14} {p['rating']:<6} "
                      f"entry≤{p['entry_zone_high']} SL={p['stop_loss']} "
                      f"T1={p['target_1']} "
                      f"{'[DRY]' if p['is_dry_run'] else ''}")

            positions = conn.execute(
                "SELECT id, ticker, quantity, entry_price, exit_price, status, "
                "pnl, exit_reason FROM positions WHERE date = ? "
                "ORDER BY opened_at",
                (ist_today,),
            ).fetchall()
            ok(f"positions rows    = {len(positions)} "
               f"({sum(1 for p in positions if p['status'] == 'open')} open, "
               f"{sum(1 for p in positions if p['status'] == 'closed')} closed)")
            for p in positions:
                pnl = f"₹{p['pnl']:+.2f}" if p['pnl'] is not None else "—"
                print(f"      • #{p['id']} {p['ticker']:<14} qty={p['quantity']} "
                      f"entry={p['entry_price']} exit={p['exit_price']} "
                      f"{p['status']:<7} pnl={pnl} reason={p['exit_reason'] or '—'}")

            dm = conn.execute(
                "SELECT * FROM daily_metrics WHERE date = ?", (ist_today,)
            ).fetchone()
            if dm:
                d = dict(dm)
                ok(f"daily_metrics row found:")
                for k in ("capital", "start_capital", "free_cash", "invested",
                          "pending_reserved", "daily_pnl", "is_finalized"):
                    print(f"      • {k:<20} = {d.get(k)}")
            else:
                warn(f"no daily_metrics row for {ist_today} yet "
                     f"(expected if pipeline hasn't reached `waiting` today)")

            cl = conn.execute(
                "SELECT at, trigger, current_value, free_cash, realized_pnl, "
                "unrealized_pnl, open_positions_count FROM capital_log "
                "WHERE date = ? ORDER BY at",
                (ist_today,),
            ).fetchall()
            ok(f"capital_log rows  = {len(cl)}")
            for r in cl[-10:]:
                print(f"      • {r['at']} trig={r['trigger']:<20} "
                      f"cur={r['current_value']} free={r['free_cash']} "
                      f"realized={r['realized_pnl']} "
                      f"unreal={r['unrealized_pnl']} open={r['open_positions_count']}")
            if len(cl) > 10:
                print(f"      … ({len(cl) - 10} earlier rows hidden)")
        finally:
            conn.close()
    except Exception:
        bad(f"today's row inspection failed:\n{traceback.format_exc()}")
        fail("today_rows")

    # ------------------------------------------------------------------ in-process API call
    section("API endpoints (in-process, bypassing any proxy)")
    try:
        # Build a transient FastAPI app and hit the endpoints via TestClient —
        # no network involved, no Zscaler, no built dist needed.
        from fastapi.testclient import TestClient
        from tradingagents.web.app import create_app

        app = create_app()
        with TestClient(app) as client:
            # /api/today (no date — should default to IST today)
            r = client.get("/api/today")
            if r.status_code != 200:
                bad(f"/api/today no-date returned {r.status_code}: "
                    f"{r.text[:200]}")
                fail("/api/today no-date")
            else:
                body = r.json()
                if body.get("date") == ist_today:
                    ok(f"/api/today default date = {body['date']} (matches IST)")
                else:
                    bad(f"/api/today default date = {body.get('date')} "
                        f"(expected {ist_today})")
                    fail("/api/today default date")
                portfolio = body.get("portfolio", {})
                ok(f"      portfolio.current_value = {portfolio.get('current_value')}")
                ok(f"      portfolio.start_capital = {portfolio.get('start_capital')}")
                ok(f"      portfolio.free_cash     = {portfolio.get('free_cash')}")
                ok(f"      portfolio.realized_pnl  = {portfolio.get('realized_pnl')}")
                ok(f"      portfolio.source        = {portfolio.get('source')}")
                ok(f"      trade_plans count       = {len(body.get('trade_plans', []))}")
                ok(f"      open_positions count    = {len(body.get('open_positions', []))}")

            # /api/today?date=YYYY-MM-DD (explicit)
            r = client.get(f"/api/today?date={ist_today}")
            if r.status_code == 200 and r.json().get("date") == ist_today:
                ok(f"/api/today?date={ist_today} ok")
            else:
                bad(f"/api/today?date={ist_today} failed")
                fail("/api/today explicit")

            # /api/capital/log
            r = client.get(f"/api/capital/log?date={ist_today}")
            if r.status_code == 200:
                rows = r.json().get("rows", [])
                ok(f"/api/capital/log rows = {len(rows)}")
            else:
                bad(f"/api/capital/log returned {r.status_code}")
                fail("/api/capital/log")

            # /api/pipeline/state
            r = client.get("/api/pipeline/state")
            if r.status_code == 200:
                ps = r.json()
                ok(f"/api/pipeline/state state={ps.get('state')} "
                   f"trade_date={ps.get('trade_date')} "
                   f"last_heartbeat_at={ps.get('last_heartbeat_at')}")
            else:
                bad(f"/api/pipeline/state returned {r.status_code}")
                fail("/api/pipeline/state")

            # /api/global-summary
            r = client.get("/api/global-summary")
            if r.status_code == 200:
                gs = r.json()
                ok(f"/api/global-summary current_capital={gs.get('current_capital')} "
                   f"days_traded={gs.get('days_traded')}")
            else:
                bad(f"/api/global-summary returned {r.status_code}")
                fail("/api/global-summary")
    except Exception:
        bad(f"API inspection failed:\n{traceback.format_exc()}")
        fail("api")

    # ------------------------------------------------------------------ front-end bundle
    section("Frontend bundle")
    try:
        import os
        repo = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
        dist = os.path.join(repo, "frontend", "dist")
        if not os.path.isdir(dist):
            warn(f"no frontend/dist directory at {dist} — "
                 f"run `cd frontend && npm run build`")
        else:
            idx = os.path.join(dist, "index.html")
            mtime = datetime.fromtimestamp(os.path.getmtime(idx))
            age_hours = (datetime.now() - mtime).total_seconds() / 3600
            print(f"      • frontend/dist/index.html last built: "
                  f"{mtime} ({age_hours:.1f}h ago)")
            assets = os.listdir(os.path.join(dist, "assets"))
            print(f"      • bundle file(s): {assets}")
            if age_hours > 24:
                warn("dist is more than 24h old — rebuild if you've made "
                     "frontend changes since")
            else:
                ok("dist is fresh")
    except Exception:
        bad(f"frontend dist check failed:\n{traceback.format_exc()}")
        fail("frontend dist")

    # ------------------------------------------------------------------ summary
    section("Summary")
    if not _failures:
        print(f"{GREEN}All checks passed.{END}")
        return 0
    print(f"{RED}{len(_failures)} check(s) failed:{END}")
    for f in _failures:
        print(f"  - {f}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
