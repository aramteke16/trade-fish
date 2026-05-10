"""Endpoints for editing trade-plan flags (not for creating trades).

Currently exposes:
  POST /api/trades/{plan_id}/exclude-from-feedback   toggle the
        ``exclude_from_feedback`` flag so the EOD reflection sweep skips
        this trade. Useful when a trade outcome is dominated by a one-off
        market event you don't want the memory log to over-fit on.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..database import get_conn

router = APIRouter()


class ExcludeRequest(BaseModel):
    exclude: bool = True


@router.post("/trades/{plan_id}/exclude-from-feedback")
def toggle_exclude(plan_id: int, body: ExcludeRequest):
    conn = get_conn()
    try:
        row = conn.execute("SELECT id FROM trade_plans WHERE id = ?", (plan_id,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail=f"trade_plan {plan_id} not found")
        conn.execute(
            "UPDATE trade_plans SET exclude_from_feedback = ? WHERE id = ?",
            (1 if body.exclude else 0, plan_id),
        )
        conn.commit()
    finally:
        conn.close()
    return {"id": plan_id, "exclude_from_feedback": body.exclude}
