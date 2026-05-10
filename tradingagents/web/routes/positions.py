from datetime import datetime
from typing import Optional

import yfinance as yf
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..database import get_conn, get_positions, update_position_exit
from tradingagents.dataflows.indian_market import IST

router = APIRouter()


class ExitRequest(BaseModel):
    reason: str = "manual_exit"


@router.get("/positions")
def list_positions():
    open_pos = get_positions(status="open")
    closed_pos = get_positions(status="closed")
    return {"open": open_pos, "closed": closed_pos}


@router.post("/positions/{position_id}/exit")
def exit_position(position_id: int, body: ExitRequest):
    """Manually exit an open position at the current market price.

    Tries the in-memory PaperTrader first (so trailing-stop state stays
    consistent). Falls back to a direct DB update if no active PaperTrader
    exists (e.g. pipeline is idle but stale open rows remain).
    """
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT * FROM positions WHERE id = ? AND status = 'open'", (position_id,)
        ).fetchone()
    finally:
        conn.close()

    if row is None:
        raise HTTPException(status_code=404, detail="Open position not found")

    ticker = row["ticker"]
    date = row["date"]
    entry_price = row["entry_price"]
    quantity = row["quantity"]

    try:
        info = yf.Ticker(ticker).fast_info
        price = float(info.get("lastPrice") or info.get("regularMarketPrice") or info.get("previousClose") or 0)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Could not fetch price for {ticker}: {e}")

    if price <= 0:
        raise HTTPException(status_code=502, detail=f"Invalid price for {ticker}")

    now = datetime.now(IST)

    # Try the in-memory paper trader for consistency with the monitor
    from tradingagents.pipeline.dispatcher import get_active_paper_trader
    paper_trader = get_active_paper_trader()
    if paper_trader and ticker in paper_trader.position_tracker.open_positions:
        events = paper_trader.force_exit_position(ticker, price, body.reason, now)
        pnl = events[0].get("pnl") if events else None
        pnl_pct = events[0].get("pnl_pct") if events else None
    else:
        pnl = (price - entry_price) * quantity if entry_price and quantity else None
        pnl_pct = ((price - entry_price) / entry_price * 100) if entry_price else None

    update_position_exit(ticker, date, {
        "exit_price": price,
        "exit_reason": body.reason,
        "pnl": pnl,
        "pnl_pct": pnl_pct,
        "closed_at": now.isoformat(),
    })

    return {
        "id": position_id,
        "ticker": ticker,
        "exit_price": price,
        "reason": body.reason,
        "pnl": pnl,
        "pnl_pct": pnl_pct,
    }
