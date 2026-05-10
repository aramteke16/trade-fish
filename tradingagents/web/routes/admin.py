"""Admin endpoints — paper-trading-only operations.

Currently exposes:
  POST /api/admin/reset-capital   reset paper-trading capital to a fresh
                                  starting amount (default = initial_capital
                                  from app_config). Hidden in the UI when
                                  paper_mode=False.

These should NEVER be exposed once a real broker SDK is wired in.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..config_service import load_config
from ..database import insert_daily_metrics

router = APIRouter()


class ResetCapitalRequest(BaseModel):
    capital: Optional[float] = None  # None → use initial_capital from config


@router.post("/admin/reset-capital")
def reset_capital(body: ResetCapitalRequest):
    """Force today's `daily_metrics` capital to the requested amount.

    The next pipeline run reads ``get_latest_capital(default=initial_capital)``
    so writing a fresh row here makes that value the new starting point.
    Refused when ``paper_mode`` is False — too dangerous for live trading.
    """
    cfg = load_config()
    if not bool(cfg.get("paper_mode", True)):
        raise HTTPException(
            status_code=403,
            detail="reset-capital is disabled when paper_mode=False",
        )
    amount = body.capital if body.capital is not None else float(cfg.get("initial_capital", 20000))
    if amount <= 0:
        raise HTTPException(status_code=400, detail="capital must be positive")
    today = datetime.now().strftime("%Y-%m-%d")
    insert_daily_metrics({
        "date": today,
        "capital": amount,
        "daily_pnl": 0.0,
        "daily_return_pct": 0.0,
        "total_trades": 0,
        "win_rate": 0.0,
        "max_drawdown_pct": 0.0,
        "notes": "manual reset via /api/admin/reset-capital",
    })
    return {"capital": amount, "date": today}
