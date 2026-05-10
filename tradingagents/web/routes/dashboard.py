from datetime import datetime
from fastapi import APIRouter
from typing import Optional

from ..database import get_trade_plans, get_positions, get_daily_metrics

router = APIRouter()


@router.get("/today")
def get_today_dashboard(date: Optional[str] = None):
    if not date:
        date = datetime.now().strftime("%Y-%m-%d")
    plans = get_trade_plans(date)
    open_pos = get_positions(status="open")
    metrics_rows = get_daily_metrics()
    latest_capital = metrics_rows[0]["capital"] if metrics_rows else 20000.0
    latest_pnl = metrics_rows[0]["daily_pnl"] if metrics_rows else 0.0
    return {
        "date": date,
        "trade_plans": plans,
        "open_positions": open_pos,
        "capital": latest_capital,
        "daily_pnl": latest_pnl,
    }
