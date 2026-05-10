"""REST API for the runtime config service.

Endpoints under ``/api/config``:

  GET    /                              all keys, grouped by category, secrets masked
  GET    /{category}                    one category (e.g. /llm, /risk)
  GET    /key/{key}                     one key (still masked if secret)
  PATCH  /                              bulk update {key: value, ...}
  PATCH  /key/{key}                     single key update {value: ...}
  POST   /reset                         reset entire app_config to seed defaults
  POST   /reset/{category}              reset just one category
  GET    /history                       audit trail of recent changes

Note the ``/key/{key}`` shape — we use it to disambiguate single-key paths
from category paths (``/llm`` vs ``/key/moonshot_api_key``). FastAPI router
ordering matters here; the ``/{category}`` route would otherwise eat single
keys.
"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Body, HTTPException
from pydantic import BaseModel

from ..config_service import (
    ConfigError,
    get_config_grouped,
    get_config_value,
    get_recent_changes,
    reset_config,
    set_config,
    set_config_bulk,
)
from tradingagents.pipeline.dispatcher import _reschedule

router = APIRouter()


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------


class SingleUpdate(BaseModel):
    """Body shape for PATCH /api/config/key/{key}."""

    value: Any


# ---------------------------------------------------------------------------
# GET endpoints
# ---------------------------------------------------------------------------


@router.get("/config")
def get_all_config():
    """Return every config key, grouped by category, secrets masked."""
    return get_config_grouped()


@router.get("/config/key/{key}")
def get_one_key(key: str):
    """Return a single key, with its metadata. Secrets are masked."""
    grouped = get_config_grouped()
    for items in grouped.values():
        for item in items:
            if item["key"] == key:
                return item
    raise HTTPException(status_code=404, detail=f"Unknown config key: {key!r}")


@router.get("/config/history")
def get_history(limit: int = 50):
    """Recent edits, newest first. Used by the UI history panel."""
    return {"changes": get_recent_changes(limit=limit)}


@router.get("/config/{category}")
def get_one_category(category: str):
    """Return all keys in a single category."""
    result = get_config_grouped(category=category)
    if not result.get(category):
        raise HTTPException(status_code=404, detail=f"Unknown category: {category!r}")
    return result


# ---------------------------------------------------------------------------
# PATCH endpoints
# ---------------------------------------------------------------------------


@router.patch("/config/key/{key}")
def patch_one_key(key: str, body: SingleUpdate):
    """Update a single key. Validates key exists and value matches expected type."""
    try:
        new_value = set_config(key, body.value)
    except ConfigError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if key.startswith("dispatcher_"):
        _reschedule(1)
    return {"key": key, "value": new_value}


@router.patch("/config")
def patch_bulk(updates: dict = Body(...)):
    """Update multiple keys atomically. Body is a flat ``{key: value}`` dict.

    On any validation error the entire batch is rolled back.
    """
    try:
        applied = set_config_bulk(updates)
    except ConfigError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if any(k.startswith("dispatcher_") for k in applied):
        _reschedule(1)
    return {"applied": applied, "count": len(applied)}


# ---------------------------------------------------------------------------
# POST endpoints
# ---------------------------------------------------------------------------


@router.post("/config/reset")
def post_reset_all():
    """Reset every config key back to its DEFAULT_CONFIG seed value.
    Records a row in ``config_changes`` for each key that actually moved."""
    n = reset_config()
    return {"reset": n}


@router.post("/config/reset/{category}")
def post_reset_category(category: str):
    """Reset only one category. Useful for fixing a single misconfigured area
    without losing edits to other categories."""
    # Validate the category exists
    grouped = get_config_grouped(category=category)
    if not grouped.get(category):
        raise HTTPException(status_code=404, detail=f"Unknown category: {category!r}")
    n = reset_config(category=category)
    return {"reset": n, "category": category}
