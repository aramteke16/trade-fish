from datetime import datetime
from fastapi import APIRouter
from typing import Optional

from tradingagents.dataflows.indian_market import IST
from ..database import get_trade_plans, get_positions, get_daily_metrics
from ..config_service import load_config
from .. import capital_service

router = APIRouter()


def _today_ist() -> str:
    """IST-anchored YYYY-MM-DD so default-date endpoints always match the
    trading day, never the server-local (often UTC) date."""
    return datetime.now(IST).strftime("%Y-%m-%d")


def _live_capital_state(date: str) -> dict:
    """Return today's capital buckets, preferring live in-memory state.

    Lookup order:
      1. Active PaperTrader (covers pending order reservations live).
      2. Persisted daily_metrics row (set by capital_service snapshot).
      3. Computed from positions + last-known EOD capital (legacy fallback).
    """
    seed_capital = float(load_config().get("initial_capital", 20000))

    try:
        from tradingagents.pipeline.dispatcher import get_active_paper_trader
        pt = get_active_paper_trader()
    except Exception:
        pt = None

    if pt is not None:
        state = pt.get_capital_state()
        return {
            "seed_capital": seed_capital,
            "start_capital": state["start_capital"],
            "current_value": state["current_value"],
            "free_cash": state["free_cash"],
            "invested": state["invested"],
            "pending_reserved": state["pending_reserved"],
            "realized_pnl": state["realized_pnl"],
            "is_finalized": False,
            "source": "live",
        }

    row = capital_service.get_today(date)
    if row and row.get("start_capital") is not None:
        start = float(row["start_capital"])
        pnl = float(row.get("daily_pnl") or 0)
        return {
            "seed_capital": seed_capital,
            "start_capital": start,
            "current_value": start + pnl,
            "free_cash": float(row.get("free_cash") or 0),
            "invested": float(row.get("invested") or 0),
            "pending_reserved": float(row.get("pending_reserved") or 0),
            "realized_pnl": pnl,
            "is_finalized": bool(row.get("is_finalized")),
            "source": "snapshot",
        }

    # Legacy fallback: derive from positions + latest EOD capital row.
    metrics_rows = get_daily_metrics()
    start = float(metrics_rows[0]["capital"]) if metrics_rows else seed_capital
    open_pos = get_positions(status="open")
    closed_today = [p for p in get_positions(status="closed") if p.get("date") == date]
    invested = sum(
        (p.get("entry_price", 0) or 0) * (p.get("quantity", 0) or 0)
        for p in open_pos if p.get("date") == date
    )
    realized = sum((p.get("pnl", 0) or 0) for p in closed_today)
    return {
        "seed_capital": seed_capital,
        "start_capital": start,
        "current_value": start + realized,
        "free_cash": max(0.0, start + realized - invested),
        "invested": invested,
        "pending_reserved": 0.0,
        "realized_pnl": realized,
        "is_finalized": False,
        "source": "legacy",
    }


@router.get("/global-summary")
def get_global_summary():
    cfg = load_config()
    metrics = get_daily_metrics()
    closed = [p for p in get_positions(status="closed") if p.get("pnl") is not None]

    initial_capital = float(cfg.get("initial_capital", 20000))
    current_capital = metrics[0]["capital"] if metrics else initial_capital
    lifetime_pnl = sum(m.get("daily_pnl", 0) or 0 for m in metrics)
    lifetime_pnl_pct = (lifetime_pnl / initial_capital * 100) if initial_capital else 0

    wins = [t for t in closed if (t.get("pnl") or 0) > 0]
    win_rate_pct = (len(wins) / len(closed) * 100) if closed else 0

    best_trade = None
    if closed:
        best = max(closed, key=lambda t: t.get("pnl") or 0)
        if (best.get("pnl") or 0) > 0:
            best_trade = {"ticker": best.get("ticker"), "pnl": best["pnl"]}

    return {
        "current_capital": current_capital,
        "initial_capital": initial_capital,
        "lifetime_pnl": lifetime_pnl,
        "lifetime_pnl_pct": lifetime_pnl_pct,
        "days_traded": len(metrics),
        "total_trades": len(closed),
        "win_rate_pct": win_rate_pct,
        "best_trade": best_trade,
        "paper_mode": True,
    }


@router.get("/history/loss-attribution")
def get_loss_attribution(days: int = 30):
    metrics = get_daily_metrics()
    recent = metrics[:days]
    closed_all = get_positions(status="closed")
    closed_by_date = {}
    for p in closed_all:
        d = p.get("date")
        if d:
            closed_by_date.setdefault(d, []).append(p)

    days_out = []
    for m in recent:
        date = m.get("date", "")
        day_trades = closed_by_date.get(date, [])
        worst = None
        if day_trades:
            w = min(day_trades, key=lambda t: t.get("pnl") or 0)
            if (w.get("pnl") or 0) < 0:
                worst = {"ticker": w.get("ticker"), "pnl": w["pnl"]}
        days_out.append({
            "date": date,
            "daily_pnl": m.get("daily_pnl", 0),
            "capital": m.get("capital"),
            "worst_trade": worst,
        })
    return {"days": days_out}


@router.get("/capital/log")
def get_capital_log(date: Optional[str] = None, limit: int = 200):
    """Return the intraday capital snapshot log for ``date`` (newest first).

    Each row is one monitor tick (or capital event) with trigger reason,
    free_cash, invested, pending, realized + unrealized P&L, and the open
    positions count at that moment.
    """
    if not date:
        date = _today_ist()
    return {"date": date, "rows": capital_service.get_log(date, limit=limit)}


@router.get("/today")
def get_today_dashboard(date: Optional[str] = None):
    if not date:
        date = _today_ist()
    plans = get_trade_plans(date)
    open_pos = get_positions(status="open")
    closed_today = [p for p in get_positions(status="closed") if p.get("date") == date]

    cap = _live_capital_state(date)

    return {
        "date": date,
        "trade_plans": plans,
        "open_positions": open_pos,
        "capital": cap["current_value"],
        "daily_pnl": cap["realized_pnl"],
        "portfolio": {
            "seed_capital": cap["seed_capital"],
            "start_capital": cap["start_capital"],
            "current_value": cap["current_value"],
            "initial_capital": cap["start_capital"],
            "invested": cap["invested"],
            "pending_reserved": cap["pending_reserved"],
            "free_cash": cap["free_cash"],
            "realized_pnl": cap["realized_pnl"],
            "is_finalized": cap["is_finalized"],
            "source": cap["source"],
            "open_positions_count": len(open_pos),
            "closed_trades_count": len(closed_today),
        },
    }
