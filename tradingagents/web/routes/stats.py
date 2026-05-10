"""LLM usage stats endpoint.

The pipeline writes per-stage rows to the ``token_usage`` table after
each phase (analysts, debates, PM, news monitor, EOD reflection). This
router exposes them to the UI Token Usage panel.

Cost is intentionally not computed — the user opted to display token
counts only.
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter

from ..token_usage import get_usage

router = APIRouter()


@router.get("/stats/tokens")
def get_token_stats(date: Optional[str] = None):
    """Per-stage token usage.

    With ``date=YYYY-MM-DD`` set: every row for that date.
    Without: aggregated (date, stage) totals for the last 90 days,
    suitable for a multi-day token chart.
    """
    return {"date": date, "rows": get_usage(date=date)}
