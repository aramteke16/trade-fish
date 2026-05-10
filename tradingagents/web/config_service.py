"""Runtime config service backed by SQLite.

This is the single read/write surface for everything the user can tune. The
pipeline calls ``load_config()`` at the start of each run (and the live
monitor re-calls it on every poll) so changes made through the REST API
take effect without a redeploy.

Three classes of caller:

  - **Pipeline / agents** — call ``load_config()``. Get back a plain ``dict``
    that's a drop-in replacement for the old ``DEFAULT_CONFIG`` dict. Storage
    paths and secrets respect env-var precedence so existing
    ``TRADINGAGENTS_HOME`` / ``MOONSHOT_API_KEY`` setups keep working.

  - **REST handlers** — call ``get_config_grouped()`` (for GET responses,
    secrets masked) or ``set_config()`` / ``reset_config()`` for mutations.
    The PATCH handler should call ``set_config()`` which writes to both
    ``app_config`` and ``config_changes`` (audit trail).

  - **CLI per-run overrides** — apply a dict diff to the loaded config in
    memory, don't persist. The next run goes back to the DB value.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from typing import Any, Iterable, Optional

from tradingagents.default_config import CONFIG_METADATA, DEFAULT_CONFIG
from tradingagents.web.database import _seed_default_config_if_empty, get_conn

logger = logging.getLogger(__name__)

# Storage-category keys that respect env-var override even after the value
# is set in the DB. These are the paths a cloud deployment may need to point
# at a mounted volume / object store regardless of what's in the DB.
_PATH_ENV_OVERRIDES = {
    "results_dir": "TRADINGAGENTS_RESULTS_DIR",
    "data_cache_dir": "TRADINGAGENTS_CACHE_DIR",
    "memory_log_path": "TRADINGAGENTS_MEMORY_LOG_PATH",
    "reports_dir": "TRADINGAGENTS_REPORTS_DIR",
}

# Secret-category keys that respect env-var override. The env-var pattern
# matches existing .env conventions: e.g. moonshot_api_key ↔ MOONSHOT_API_KEY.
def _secret_env_var(key: str) -> str:
    """Translate a config key to its conventional env-var name."""
    return key.upper()


# ---------------------------------------------------------------------------
# Public read API
# ---------------------------------------------------------------------------


def load_config() -> dict:
    """Build the runtime config dict the pipeline uses.

    Reads every row from ``app_config``, JSON-decodes values, applies env-var
    precedence for paths and secrets, and returns a plain dict. Always queries
    the DB fresh — no caching, per the user's "always query DB to get the
    latest info" decision.

    Falls back to ``DEFAULT_CONFIG`` if the DB is unreachable (e.g. file lock
    during pytest tear-down) so the pipeline never crashes on a config read.
    """
    try:
        conn = get_conn()
        # Defensive: ensure table + rows exist. _seed handles missing-rows;
        # init_db() handles missing-table. If init_db() hasn't run yet (rare
        # ordering edge-case), we run it inline.
        _ensure_table_seeded(conn)
        rows = conn.execute(
            "SELECT key, value, category, is_secret FROM app_config"
        ).fetchall()
        conn.close()
    except Exception as e:
        logger.warning("config DB unreachable (%s); using DEFAULT_CONFIG", e)
        return dict(DEFAULT_CONFIG)

    cfg: dict = {}
    for row in rows:
        key = row["key"]
        try:
            value = json.loads(row["value"])
        except (json.JSONDecodeError, TypeError):
            value = row["value"]
        cfg[key] = value

    # Layer 2: env-var precedence for paths.
    for key, env_var in _PATH_ENV_OVERRIDES.items():
        env_val = os.getenv(env_var)
        if env_val:
            cfg[key] = env_val

    # Layer 3: env-var precedence for secrets (existing .env workflows).
    for key, meta in CONFIG_METADATA.items():
        if meta.get("is_secret"):
            env_val = os.getenv(_secret_env_var(key))
            if env_val:
                cfg[key] = env_val

    # Defensive: any DEFAULT_CONFIG key missing from the DB (corruption,
    # manual sqlite mucking) falls back to its default.
    for key, default in DEFAULT_CONFIG.items():
        cfg.setdefault(key, default)

    return cfg


def get_config_grouped(category: Optional[str] = None) -> dict:
    """Return config keys grouped by category, secrets masked, for the UI.

    Shape::

        {
          "allocator": [
            {"key": "top_k_positions", "value": 3, "is_secret": False, "description": "..."},
            ...
          ],
          "llm": [...],
          ...
        }

    When ``category`` is provided, returns only that category's list (not
    nested under a category dict).
    """
    conn = get_conn()
    _ensure_table_seeded(conn)
    if category:
        rows = conn.execute(
            "SELECT key, value, category, is_secret, description, updated_at "
            "FROM app_config WHERE category = ? ORDER BY key",
            (category,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT key, value, category, is_secret, description, updated_at "
            "FROM app_config ORDER BY category, key"
        ).fetchall()
    conn.close()

    items = []
    for row in rows:
        try:
            value = json.loads(row["value"])
        except (json.JSONDecodeError, TypeError):
            value = row["value"]
        is_secret = bool(row["is_secret"])
        # Mask secrets but distinguish "set" from "unset" for the UI.
        display_value = value
        if is_secret:
            display_value = "***" if value else None
        item = {
            "key": row["key"],
            "value": display_value,
            "is_secret": is_secret,
            "category": row["category"],
            "description": row["description"],
            "updated_at": row["updated_at"],
        }
        meta = CONFIG_METADATA.get(row["key"], {})
        if meta.get("hidden"):
            continue
        if "options" in meta:
            item["options"] = meta["options"]
        if "provider_models" in meta:
            item["provider_models"] = meta["provider_models"]
        if "input_type" in meta:
            item["input_type"] = meta["input_type"]
        items.append(item)

    if category:
        return {category: items}

    grouped: dict[str, list] = {}
    for item in items:
        grouped.setdefault(item["category"], []).append(item)
    return grouped


def get_config_value(key: str) -> Any:
    """Return the unmasked value for a single key. Used internally by the
    pipeline; never expose this directly via REST."""
    if key not in CONFIG_METADATA and key not in DEFAULT_CONFIG:
        raise KeyError(f"Unknown config key: {key!r}")
    conn = get_conn()
    _ensure_table_seeded(conn)
    row = conn.execute(
        "SELECT value FROM app_config WHERE key = ?", (key,)
    ).fetchone()
    conn.close()
    if row is None:
        return DEFAULT_CONFIG.get(key)
    try:
        return json.loads(row["value"])
    except (json.JSONDecodeError, TypeError):
        return row["value"]


# ---------------------------------------------------------------------------
# Public write API
# ---------------------------------------------------------------------------


class ConfigError(ValueError):
    """Raised on PATCH validation failures. REST handlers map these to 400."""


def set_config(key: str, new_value: Any) -> Any:
    """Update a single key. Validates the key is known and the new value's
    type matches the existing type. Records the change in ``config_changes``.
    Returns the JSON-decoded new value (post-coercion).

    Raises ConfigError on:
      - Unknown key (not in CONFIG_METADATA).
      - Type mismatch (e.g. PATCH ``top_k_positions: "abc"``).
    """
    if key not in CONFIG_METADATA:
        raise ConfigError(f"Unknown config key: {key!r}")

    # Type coercion / validation. We intentionally allow None for any key
    # whose default is None (so the user can clear an optional setting).
    default = DEFAULT_CONFIG.get(key)
    expected_type = type(default) if default is not None else None
    if new_value is not None and expected_type is not None:
        # Bools are ints in Python — guard explicitly so PATCH 1 doesn't
        # become PATCH True for a numeric field.
        if expected_type is bool and not isinstance(new_value, bool):
            raise ConfigError(
                f"{key!r} expects bool, got {type(new_value).__name__}"
            )
        if expected_type in (int, float) and isinstance(new_value, bool):
            raise ConfigError(
                f"{key!r} expects {expected_type.__name__}, got bool"
            )
        # Numbers: allow int↔float coercion in either direction.
        if expected_type in (int, float):
            if not isinstance(new_value, (int, float)):
                raise ConfigError(
                    f"{key!r} expects number, got {type(new_value).__name__}"
                )
            new_value = expected_type(new_value)
        elif expected_type is str:
            if not isinstance(new_value, str):
                raise ConfigError(
                    f"{key!r} expects string, got {type(new_value).__name__}"
                )
        elif expected_type is dict:
            if not isinstance(new_value, dict):
                raise ConfigError(
                    f"{key!r} expects object, got {type(new_value).__name__}"
                )

    new_value_json = json.dumps(new_value)

    conn = get_conn()
    _ensure_table_seeded(conn)
    old_row = conn.execute(
        "SELECT value FROM app_config WHERE key = ?", (key,)
    ).fetchone()
    old_value_json = old_row["value"] if old_row else None

    # No-op if unchanged — avoids audit-trail noise.
    if old_value_json == new_value_json:
        conn.close()
        return new_value

    conn.execute(
        "UPDATE app_config SET value = ?, updated_at = CURRENT_TIMESTAMP WHERE key = ?",
        (new_value_json, key),
    )
    conn.execute(
        "INSERT INTO config_changes (key, old_value, new_value) VALUES (?, ?, ?)",
        (key, old_value_json, new_value_json),
    )
    conn.commit()
    conn.close()
    logger.info("config: %s changed (%s → %s)", key, old_value_json, new_value_json)
    return new_value


def set_config_bulk(updates: dict) -> dict:
    """Apply multiple key=value updates atomically (one transaction).

    Returns a dict of {key: new_value} for successfully applied changes.
    Validation is per-key and stops on the first error — partial application
    is rolled back so the DB never sees a half-applied PATCH.
    """
    if not updates:
        return {}

    # Pre-validate everything before opening the transaction so we don't
    # have to roll back half-way through.
    for key, value in updates.items():
        if key not in CONFIG_METADATA:
            raise ConfigError(f"Unknown config key: {key!r}")

    applied: dict = {}
    conn = get_conn()
    _ensure_table_seeded(conn)
    try:
        conn.execute("BEGIN")
        for key, value in updates.items():
            # Reuse single-key validation by inlining its core (without its
            # own transaction).
            old_row = conn.execute(
                "SELECT value FROM app_config WHERE key = ?", (key,)
            ).fetchone()
            old_value_json = old_row["value"] if old_row else None

            # Run the same type checks as set_config().
            default = DEFAULT_CONFIG.get(key)
            expected_type = type(default) if default is not None else None
            coerced = value
            if value is not None and expected_type is not None:
                if expected_type is bool and not isinstance(value, bool):
                    raise ConfigError(f"{key!r} expects bool")
                if expected_type in (int, float) and isinstance(value, bool):
                    raise ConfigError(f"{key!r} expects {expected_type.__name__}")
                if expected_type in (int, float):
                    if not isinstance(value, (int, float)):
                        raise ConfigError(f"{key!r} expects number")
                    coerced = expected_type(value)
                elif expected_type is str and not isinstance(value, str):
                    raise ConfigError(f"{key!r} expects string")
                elif expected_type is dict and not isinstance(value, dict):
                    raise ConfigError(f"{key!r} expects object")

            new_value_json = json.dumps(coerced)
            if new_value_json != old_value_json:
                conn.execute(
                    "UPDATE app_config SET value = ?, updated_at = CURRENT_TIMESTAMP WHERE key = ?",
                    (new_value_json, key),
                )
                conn.execute(
                    "INSERT INTO config_changes (key, old_value, new_value) VALUES (?, ?, ?)",
                    (key, old_value_json, new_value_json),
                )
                applied[key] = coerced
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return applied


def reset_config(category: Optional[str] = None) -> int:
    """Reset config rows back to their DEFAULT_CONFIG values.

    When ``category`` is provided, only that category resets. Otherwise the
    entire app_config table is wiped and re-seeded.

    Returns the number of rows that were reset (i.e. that differed from the
    seed default).
    """
    conn = get_conn()
    if category:
        keys = [k for k, meta in CONFIG_METADATA.items() if meta.get("category") == category]
    else:
        keys = list(CONFIG_METADATA.keys())

    reset_count = 0
    try:
        conn.execute("BEGIN")
        for key in keys:
            default_json = json.dumps(DEFAULT_CONFIG.get(key))
            old_row = conn.execute(
                "SELECT value FROM app_config WHERE key = ?", (key,)
            ).fetchone()
            old_value_json = old_row["value"] if old_row else None
            if old_value_json != default_json:
                conn.execute(
                    "UPDATE app_config SET value = ?, updated_at = CURRENT_TIMESTAMP WHERE key = ?",
                    (default_json, key),
                )
                conn.execute(
                    "INSERT INTO config_changes (key, old_value, new_value) VALUES (?, ?, ?)",
                    (key, old_value_json, default_json),
                )
                reset_count += 1
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    logger.info(
        "config reset: %d row(s) restored to default (category=%s)",
        reset_count, category or "all",
    )
    return reset_count


def get_recent_changes(limit: int = 50) -> list[dict]:
    """Return the audit trail of recent config changes for the UI history view."""
    conn = get_conn()
    rows = conn.execute(
        "SELECT key, old_value, new_value, changed_at "
        "FROM config_changes ORDER BY changed_at DESC LIMIT ?",
        (limit,),
    ).fetchall()
    conn.close()
    items = []
    for row in rows:
        is_secret = CONFIG_METADATA.get(row["key"], {}).get("is_secret", False)
        items.append({
            "key": row["key"],
            "old_value": "***" if is_secret else row["old_value"],
            "new_value": "***" if is_secret else row["new_value"],
            "changed_at": row["changed_at"],
        })
    return items


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _ensure_table_seeded(conn: sqlite3.Connection) -> None:
    """Lazy fallback: if app_config is empty (init_db never ran in this
    process), seed it now. Cheap — single COUNT(*) against a tiny table.
    """
    try:
        count = conn.execute("SELECT COUNT(*) FROM app_config").fetchone()[0]
    except sqlite3.OperationalError:
        # Table doesn't exist yet — caller hasn't run init_db. Run it now.
        from tradingagents.web.database import init_db
        init_db()
        return
    if count == 0:
        _seed_default_config_if_empty(conn)
        conn.commit()
