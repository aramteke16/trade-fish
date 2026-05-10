from fastapi import APIRouter
from typing import Optional

from ..database import get_debates, get_agent_reports

router = APIRouter()


@router.get("/debates")
def list_debates(date: Optional[str] = None, ticker: Optional[str] = None):
    debates = get_debates(date=date, ticker=ticker)
    return {"debates": debates}


@router.get("/debates/{ticker}")
def get_ticker_debates(ticker: str, date: Optional[str] = None):
    debates = get_debates(date=date, ticker=ticker)
    reports = get_agent_reports(date=date, ticker=ticker)
    return {"ticker": ticker, "debates": debates, "agent_reports": reports}
