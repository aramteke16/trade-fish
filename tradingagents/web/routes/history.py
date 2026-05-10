from fastapi import APIRouter

from ..database import get_positions, get_trade_plans

router = APIRouter()


@router.get("/history")
def get_history():
    trades = get_positions()
    plans = get_trade_plans()
    return {"trades": trades, "trade_plans": plans}
