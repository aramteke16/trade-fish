"""Extract structured trade plan from Portfolio Manager's markdown output.

The PM produces a PortfolioDecision via structured output which is rendered
to markdown by render_pm_decision(). This module parses that deterministic
format back into a dict compatible with PaperTrader.place_trade_plan() and
the web database insert_trade_plan().

If the PM's output lacks numeric levels (e.g. for Hold/Sell ratings), falls
back to parsing the Trader's output (rendered by render_trader_proposal()).
"""

import re
import logging
from typing import Optional

logger = logging.getLogger(__name__)


def extract_trade_plan(ticker: str, date: str, final_state: dict, rating: str) -> dict:
    """Extract a structured trade plan dict from the graph's final state.

    Tries PM output first, then Trader output as fallback for numeric levels.
    Returns a dict suitable for PaperTrader.place_trade_plan() and insert_trade_plan().
    """
    pm_text = final_state.get("final_trade_decision", "")
    trader_text = final_state.get("trader_investment_plan", "")

    # Try PM output first (preferred source)
    plan = _parse_plan_fields(pm_text)

    # Fallback to Trader output for missing numeric fields
    if not plan.get("entry_zone_low") or not plan.get("stop_loss"):
        trader_plan = _parse_plan_fields(trader_text)
        for key in ("entry_zone_low", "entry_zone_high", "stop_loss", "target_1", "target_2",
                    "confidence_score", "position_size_pct"):
            if not plan.get(key) and trader_plan.get(key):
                plan[key] = trader_plan[key]

    # Also try to extract from free-text patterns (e.g. "$181" or "₹6,420")
    if not plan.get("entry_zone_low"):
        plan.update(_parse_freetext_levels(pm_text + "\n" + trader_text))

    plan["date"] = date
    plan["ticker"] = ticker
    plan["rating"] = rating
    plan["thesis"] = _extract_text(pm_text, r"\*\*Investment Thesis\*\*:\s*(.+?)(?:\n\n|\*\*|\Z)")
    plan["skip_rule"] = plan.get("skip_rule") or _extract_text(pm_text, r"\*\*Skip Rule\*\*:\s*(.+)")

    _REQUIRED = ("entry_zone_low", "entry_zone_high", "stop_loss", "target_1", "confidence_score")
    null_fields = [k for k in _REQUIRED if plan.get(k) is None]
    if null_fields:
        logger.warning("[plan_extractor] %s/%s: null fields %s — structured output likely failed", ticker, date, null_fields)
    else:
        logger.info("[plan_extractor] %s/%s: all fields extracted (conf=%s)", ticker, date, plan.get("confidence_score"))

    return plan


def _parse_plan_fields(text: str) -> dict:
    """Parse the deterministic markdown patterns from render_pm_decision / render_trader_proposal."""
    return {
        "entry_zone_low": _extract_entry_zone(text, group=1),
        "entry_zone_high": _extract_entry_zone(text, group=2),
        "stop_loss": _extract_float(text, r"\*\*Stop Loss\*\*:\s*₹?([\d,.]+)"),
        "target_1": _extract_float(text, r"\*\*Target 1\*\*:\s*₹?([\d,.]+)"),
        "target_2": _extract_float(text, r"\*\*Target 2\*\*:\s*₹?([\d,.]+)"),
        "confidence_score": _extract_int(text, r"\*\*Confidence\*\*:\s*(\d+)/10"),
        "position_size_pct": _extract_float(text, r"\*\*Position Size %?\*\*:\s*([\d.]+)%?"),
        "skip_rule": _extract_text(text, r"\*\*Skip Rule\*\*:\s*(.+)"),
    }


def _extract_entry_zone(text: str, group: int) -> Optional[float]:
    """Parse **Entry Zone**: ₹{low} - ₹{high}"""
    m = re.search(r"\*\*Entry Zone\*\*:\s*₹?([\d,.]+)\s*[-–]\s*₹?([\d,.]+)", text, re.IGNORECASE)
    if m:
        try:
            return float(m.group(group).replace(",", ""))
        except (ValueError, IndexError):
            pass
    return None


def _parse_freetext_levels(text: str) -> dict:
    """Attempt to extract entry/SL/target from unstructured LLM prose.

    Looks for patterns like:
    - "Entry: $181" or "entry zone of ₹6,420-₹6,480"
    - "stop-loss at ₹6,350" or "SL: 165"
    - "target of ₹6,550" or "T1: 205"
    """
    result = {}

    # Entry zone pattern: "entry zone" or "entry" followed by price range
    m = re.search(r"entry\s*(?:zone)?[:\s]*₹?\$?([\d,.]+)\s*[-–to]+\s*₹?\$?([\d,.]+)", text, re.IGNORECASE)
    if m:
        result["entry_zone_low"] = _safe_float(m.group(1))
        result["entry_zone_high"] = _safe_float(m.group(2))

    # Stop loss
    m = re.search(r"(?:stop[- ]?loss|SL)[:\s]*₹?\$?([\d,.]+)", text, re.IGNORECASE)
    if m:
        result["stop_loss"] = _safe_float(m.group(1))

    # Target 1
    m = re.search(r"(?:target\s*1|T1)[:\s]*₹?\$?([\d,.]+)", text, re.IGNORECASE)
    if m:
        result["target_1"] = _safe_float(m.group(1))

    # Target 2
    m = re.search(r"(?:target\s*2|T2)[:\s]*₹?\$?([\d,.]+)", text, re.IGNORECASE)
    if m:
        result["target_2"] = _safe_float(m.group(1))

    return result


def _extract_float(text: str, pattern: str) -> Optional[float]:
    m = re.search(pattern, text, re.IGNORECASE)
    if m:
        try:
            return float(m.group(1).replace(",", ""))
        except (ValueError, IndexError):
            pass
    return None


def _extract_int(text: str, pattern: str) -> Optional[int]:
    m = re.search(pattern, text, re.IGNORECASE)
    if m:
        try:
            return int(m.group(1))
        except (ValueError, IndexError):
            pass
    return None


def _extract_text(text: str, pattern: str) -> Optional[str]:
    m = re.search(pattern, text, re.IGNORECASE | re.DOTALL)
    if m:
        return m.group(1).strip()
    return None


def _safe_float(s: str) -> Optional[float]:
    try:
        return float(s.replace(",", ""))
    except (ValueError, TypeError):
        return None
