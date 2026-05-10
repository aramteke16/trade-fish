from fastapi import APIRouter

from ..database import get_positions

router = APIRouter()


@router.get("/positions")
def list_positions():
    open_pos = get_positions(status="open")
    closed_pos = get_positions(status="closed")
    return {"open": open_pos, "closed": closed_pos}
