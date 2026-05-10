from datetime import datetime
from fastapi import APIRouter
from typing import Optional

from ..database import get_trade_plans, get_positions, get_daily_metrics
from ..config_service import load_config

router = APIRouter()


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


@router.get("/today")
def get_today_dashboard(date: Optional[str] = None):
    if not date:
        date = datetime.now().strftime("%Y-%m-%d")
    plans = get_trade_plans(date)
    open_pos = get_positions(status="open")
    closed_today = [p for p in get_positions(status="closed") if p.get("date") == date]
    metrics_rows = get_daily_metrics()
    latest_capital = metrics_rows[0]["capital"] if metrics_rows else 20000.0
    latest_pnl = metrics_rows[0]["daily_pnl"] if metrics_rows else 0.0

    realized_pnl = sum(p.get("pnl", 0) or 0 for p in closed_today)
    invested = sum(
        (p.get("entry_price", 0) or 0) * (p.get("quantity", 0) or 0)
        for p in open_pos
    )

    return {
        "date": date,
        "trade_plans": plans,
        "open_positions": open_pos,
        "capital": latest_capital,
        "daily_pnl": latest_pnl,
        "portfolio": {
            "initial_capital": latest_capital,
            "invested": invested,
            "free_cash": latest_capital - invested + realized_pnl,
            "realized_pnl": realized_pnl,
            "open_positions_count": len(open_pos),
            "closed_trades_count": len(closed_today),
        },
    }
