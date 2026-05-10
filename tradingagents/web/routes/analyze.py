"""On-demand single-stock analysis.

Runs the full multi-agent LangGraph pipeline for one ticker (same nodes
as a daily auto-run), but in **read-only** mode:
  - The report tree is written to ``<reports_dir>/on_demand/<DATE>/<TICKER>_<HHMM>/``
    so it lives alongside but distinct from daily auto reports.
  - The ``on_demand_analyses`` table tracks status / report path.
  - We do NOT insert into ``trade_plans``, ``debates``, or ``agent_reports``
    — out-of-band runs must never accidentally feed the live trader.

Endpoints:
  POST /api/analyze              kick off (returns immediately with id)
  GET  /api/analyze              list recent runs (50)
  GET  /api/analyze/{id}         poll status / fetch report path
  GET  /api/analyze/{id}/report  return the rendered complete_report.md text

The actual run happens in a FastAPI ``BackgroundTasks`` worker. For
heavier deployments swap this for a real queue (Celery, RQ); single
droplet is fine with BackgroundTasks.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, HTTPException
from pydantic import BaseModel

from tradingagents.dataflows.indian_market import IST

from ..config_service import load_config
from ..database import get_conn
from ..token_usage import insert_usage

logger = logging.getLogger(__name__)

router = APIRouter()


_TICKER_RE = re.compile(r"^[A-Z0-9._-]{1,30}$")


class AnalyzeRequest(BaseModel):
    ticker: str
    date: Optional[str] = None


def _now_iso() -> str:
    return datetime.now(IST).isoformat()


def _insert_pending(ticker: str) -> int:
    conn = get_conn()
    try:
        cur = conn.execute(
            "INSERT INTO on_demand_analyses (ticker, requested_at, status) VALUES (?, ?, 'pending')",
            (ticker, _now_iso()),
        )
        conn.commit()
        return int(cur.lastrowid)
    finally:
        conn.close()


def _update_status(
    analysis_id: int,
    *,
    status: str,
    report_path: Optional[str] = None,
    error: Optional[str] = None,
    summary: Optional[str] = None,
) -> None:
    conn = get_conn()
    try:
        conn.execute(
            """UPDATE on_demand_analyses
               SET status = ?,
                   report_path = COALESCE(?, report_path),
                   error = ?,
                   summary = COALESCE(?, summary),
                   completed_at = CASE WHEN ? IN ('done','error') THEN ? ELSE completed_at END
               WHERE id = ?""",
            (status, report_path, error, summary, status, _now_iso(), analysis_id),
        )
        conn.commit()
    finally:
        conn.close()


def _run_full_pipeline(analysis_id: int, ticker: str, trade_date: str = "") -> None:
    """Background worker. Imports are inside the function so route import
    stays cheap and we don't need LangGraph at FastAPI startup."""
    try:
        from tradingagents.graph.trading_graph import TradingAgentsGraph
        from tradingagents.pipeline.report_writer import save_daily_analysis

        _update_status(analysis_id, status="running")
        cfg = load_config()
        date = trade_date or datetime.now(IST).strftime("%Y-%m-%d")
        hhmm = datetime.now(IST).strftime("%H%M")

        # On-demand reports go under reports_dir/on_demand/<DATE>/<TICKER>_<HHMM>/.
        # save_daily_analysis writes to <reports_dir>/<DATE>/<TICKER>/, so we
        # construct a synthetic ``reports_dir`` that aliases the on_demand
        # subtree and pass a synthetic ``date`` of "" — but cleaner is to
        # post-rename. Simpler: just call save_daily_analysis with a custom
        # base, then rename the inner ticker dir to TICKER_HHMM.
        base_reports = Path(cfg.get("reports_dir") or "~/.tradingagents/reports").expanduser()
        on_demand_root = base_reports / "on_demand"
        graph = TradingAgentsGraph(debug=False, config=cfg)
        final_state, rating = graph.propagate(ticker, date)

        # Write under on_demand/<DATE>/<TICKER>/ first (using the helper),
        # then rename to TICKER_HHMM so multiple runs the same day coexist.
        ticker_dir = save_daily_analysis(
            final_state=final_state,
            ticker=ticker,
            date=date,
            reports_dir=str(on_demand_root),
        )
        target = ticker_dir.parent / f"{ticker_dir.name}_{hhmm}"
        # If a previous run at the same minute exists (rare), append seconds.
        if target.exists():
            target = ticker_dir.parent / f"{ticker_dir.name}_{datetime.now(IST).strftime('%H%M%S')}"
        ticker_dir.rename(target)

        # Pull a one-paragraph summary for the list view — first 400 chars
        # of the PM final decision, which is the actionable bottom line.
        summary = (final_state.get("final_trade_decision") or "")[:400]

        # Persist token usage for this run, scoped to stage='on_demand'.
        # The graph's _stats_handler holds the running counts.
        try:
            handler = getattr(graph, "_stats_handler", None)
            if handler is not None:
                insert_usage(
                    date=date,
                    ticker=ticker,
                    stage="on_demand",
                    model=cfg.get("quick_think_llm"),
                    stats=handler.get_stats(),
                )
        except Exception:
            logger.debug("on_demand token usage flush failed", exc_info=True)

        rel_path = str(target.relative_to(base_reports))
        _update_status(
            analysis_id, status="done", report_path=rel_path, summary=summary,
        )
        logger.info("on-demand analyze %s (%s) → %s [%s]", ticker, analysis_id, rel_path, rating)
    except Exception as e:
        logger.exception("on-demand analyze failed for %s", ticker)
        _update_status(analysis_id, status="error", error=str(e))


@router.post("/analyze")
def post_analyze(body: AnalyzeRequest, tasks: BackgroundTasks):
    ticker = body.ticker.strip().upper()
    if not _TICKER_RE.match(ticker):
        raise HTTPException(status_code=400, detail=f"invalid ticker {ticker!r}")
    trade_date = body.date or datetime.now(IST).strftime("%Y-%m-%d")
    analysis_id = _insert_pending(ticker)
    tasks.add_task(_run_full_pipeline, analysis_id, ticker, trade_date)
    return {"analysis_id": analysis_id, "ticker": ticker, "date": trade_date, "status": "pending"}


@router.get("/analyze")
def list_analyses(limit: int = 50):
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT * FROM on_demand_analyses ORDER BY requested_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return {"analyses": [dict(r) for r in rows]}
    finally:
        conn.close()


@router.get("/analyze/{analysis_id}")
def get_analysis(analysis_id: int):
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT * FROM on_demand_analyses WHERE id = ?", (analysis_id,),
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail=f"analysis {analysis_id} not found")
        return dict(row)
    finally:
        conn.close()


@router.get("/analyze/{analysis_id}/report")
def get_analysis_report(analysis_id: int):
    """Return the rendered complete_report.md content for inline display."""
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT report_path, status FROM on_demand_analyses WHERE id = ?",
            (analysis_id,),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        raise HTTPException(status_code=404, detail=f"analysis {analysis_id} not found")
    if row["status"] != "done" or not row["report_path"]:
        raise HTTPException(status_code=409, detail=f"analysis not ready (status={row['status']})")
    cfg = load_config()
    base = Path(cfg.get("reports_dir") or "~/.tradingagents/reports").expanduser()
    report_file = base / row["report_path"] / "complete_report.md"
    if not report_file.exists():
        raise HTTPException(status_code=404, detail="report file missing on disk")
    return {"path": str(report_file), "content": report_file.read_text(encoding="utf-8")}
