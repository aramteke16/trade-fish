"""Persistence helpers for the ``token_usage`` table.

The pipeline holds a ``StatsCallbackHandler`` per stage (analysts, debates,
PM, news monitor, EOD reflection). At the end of each stage we snapshot
``handler.get_stats()`` and write a row here so the UI Token Usage panel
has something to show. Cost is intentionally NOT computed — the user
opted to display token counts only.
"""

from __future__ import annotations

from datetime import datetime
from typing import Dict, List, Optional

from .database import get_conn


def insert_usage(
    *,
    date: Optional[str] = None,
    ticker: Optional[str] = None,
    stage: str,
    model: Optional[str] = None,
    stats: Dict[str, int],
) -> None:
    """Insert one usage row from a ``StatsCallbackHandler.get_stats()`` dict.

    The handler tracks four ints (llm_calls, tool_calls, tokens_in,
    tokens_out). Missing keys default to 0 — useful for stages that don't
    use tools (e.g. PM final synthesis).
    """
    if not date:
        date = datetime.now().strftime("%Y-%m-%d")
    conn = get_conn()
    try:
        conn.execute(
            """INSERT INTO token_usage
               (date, ticker, stage, model, llm_calls, tool_calls, tokens_in, tokens_out)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                date,
                ticker,
                stage,
                model,
                int(stats.get("llm_calls", 0)),
                int(stats.get("tool_calls", 0)),
                int(stats.get("tokens_in", 0)),
                int(stats.get("tokens_out", 0)),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def get_usage(date: Optional[str] = None) -> List[dict]:
    """Return token-usage rows.

    With ``date`` set: every row for that day, ordered by created_at DESC.
    Without: aggregated totals per (date, stage) for the last 90 days,
    suitable for a multi-day token-usage chart.
    """
    conn = get_conn()
    try:
        if date:
            rows = conn.execute(
                "SELECT * FROM token_usage WHERE date = ? ORDER BY created_at DESC",
                (date,),
            ).fetchall()
            return [dict(r) for r in rows]
        rows = conn.execute(
            """SELECT date, stage,
                      SUM(llm_calls)  AS llm_calls,
                      SUM(tool_calls) AS tool_calls,
                      SUM(tokens_in)  AS tokens_in,
                      SUM(tokens_out) AS tokens_out
               FROM token_usage
               GROUP BY date, stage
               ORDER BY date DESC LIMIT 500"""
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()
