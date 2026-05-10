"""LangChain callback that mirrors LLM activity onto the FastAPI WS bus.

Attached to every TradingAgentsGraph instance alongside the existing
StatsCallbackHandler. Each LLM call broadcasts:

    - ``llm_start``  with serialized model name + run_id
    - ``llm_chunk``  on every token (when the LLM streams)
    - ``llm_end``    with token usage

If the WS event loop hasn't been set yet (CLI runs, unit tests) the
``broadcast_sync`` helper no-ops, so attaching this is always safe.

Higher-level node lifecycle events (``agent_start`` / ``agent_done``)
come from ``on_chain_start`` / ``on_chain_end`` filtered to the named
graph nodes — LangGraph emits a chain event per node.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional
from uuid import UUID

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.messages import AIMessage
from langchain_core.outputs import LLMResult

from . import websocket as ws_manager

logger = logging.getLogger(__name__)


# LangGraph node names we surface to the UI as agent lifecycle events.
# Anything not in this set is silently ignored on chain events.
_AGENT_NODES = {
    "Market Analyst",
    "Sentiment Retail Analyst",
    "Sentiment Institutional Analyst",
    "Sentiment Contrarian Analyst",
    "News Analyst",
    "Fundamentals Analyst",
    "Bull Researcher",
    "Bear Researcher",
    "Research Manager",
    "Trader",
    "Aggressive Risk Analyst",
    "Conservative Risk Analyst",
    "Neutral Risk Analyst",
    "Risk Judge",
    "Portfolio Manager",
}


class WebSocketCallbackHandler(BaseCallbackHandler):
    """Mirror LangGraph node + LLM events onto the WS bus.

    Bound to a single ticker for the duration of one ``graph.propagate``.
    The ticker is stamped on every event so the UI can route messages to
    the right "stock card" when multiple precheck workers run in parallel.
    """

    def __init__(self, ticker: str):
        super().__init__()
        self.ticker = ticker

    # ─── chain (node) lifecycle ──────────────────────────────────────────

    def on_chain_start(
        self,
        serialized: Dict[str, Any],
        inputs: Dict[str, Any],
        *,
        run_id: UUID,
        tags: Optional[List[str]] = None,
        **kwargs: Any,
    ) -> None:
        name = self._extract_node_name(serialized, kwargs)
        if name in _AGENT_NODES:
            ws_manager.broadcast_sync({
                "type": "agent_start",
                "stage": name,
                "ticker": self.ticker,
                "run_id": str(run_id),
            })

    def on_chain_end(
        self,
        outputs: Dict[str, Any],
        *,
        run_id: UUID,
        **kwargs: Any,
    ) -> None:
        name = self._extract_node_name(kwargs.get("serialized") or {}, kwargs)
        if name in _AGENT_NODES:
            # Extract output text for non-streaming (thinking) models
            text = ""
            try:
                if isinstance(outputs, dict):
                    msgs = outputs.get("messages") or outputs.get("output", [])
                    if isinstance(msgs, list) and msgs:
                        last = msgs[-1]
                        text = getattr(last, "content", "") or str(last) if last else ""
                    elif isinstance(outputs.get("output"), str):
                        text = outputs["output"]
                elif isinstance(outputs, str):
                    text = outputs
            except Exception:
                pass
            msg = {
                "type": "agent_done",
                "stage": name,
                "ticker": self.ticker,
                "run_id": str(run_id),
            }
            if text:
                msg["output"] = text[:3000]
            ws_manager.broadcast_sync(msg)

    # ─── LLM-level events (token streaming) ──────────────────────────────

    def on_llm_new_token(self, token: str, **kwargs: Any) -> None:
        if not token:
            return
        ws_manager.broadcast_sync({
            "type": "agent_chunk",
            "ticker": self.ticker,
            "delta": token,
        })

    def on_llm_end(self, response: LLMResult, **kwargs: Any) -> None:
        try:
            generation = response.generations[0][0]
        except (IndexError, TypeError):
            return
        usage = None
        if hasattr(generation, "message"):
            msg = generation.message
            if isinstance(msg, AIMessage) and hasattr(msg, "usage_metadata"):
                usage = msg.usage_metadata
        if usage:
            ws_manager.broadcast_sync({
                "type": "llm_end",
                "ticker": self.ticker,
                "tokens_in": usage.get("input_tokens", 0),
                "tokens_out": usage.get("output_tokens", 0),
            })

    # ─── helpers ─────────────────────────────────────────────────────────

    @staticmethod
    def _extract_node_name(serialized: Dict[str, Any], kwargs: Dict[str, Any]) -> str:
        """Best-effort node-name extraction from chain callback args.

        LangGraph passes the node name via `kwargs["name"]` or the last
        component of `serialized["id"]`. Fall back to an empty string so
        the membership check below is still safe.
        """
        if "name" in kwargs and isinstance(kwargs["name"], str):
            return kwargs["name"]
        ident = serialized.get("id") if isinstance(serialized, dict) else None
        if isinstance(ident, list) and ident:
            return str(ident[-1])
        return ""
