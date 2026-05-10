"""Process-wide config cache for the dataflow / agent layer.

The legacy version of this module copied ``DEFAULT_CONFIG`` once at import time
and stayed frozen for the life of the process. We keep the same public surface
(`initialize_config`, `set_config`, `get_config`) but the underlying source is
now the DB-backed config service. Each call to ``get_config`` returns the
latest values from the DB so the agent-level code automatically picks up
edits made via the REST API on the next read.

Falls back to the static ``DEFAULT_CONFIG`` if the DB is unreachable (e.g.
during unit tests that haven't initialised the schema), so import-time code
never breaks.
"""

from typing import Dict, Optional

import tradingagents.default_config as default_config

_config_override: Optional[Dict] = None


def initialize_config() -> None:
    """No-op kept for backwards compatibility — config now lives in the DB.

    Old callers used this to lazy-init the global config cache. Today the
    DB-backed ``load_config()`` does that work on demand. We keep the symbol
    so external callers don't break.
    """
    return None


def set_config(config: Dict) -> None:
    """Apply a per-process config override that wins over the DB read.

    Used by the CLI's interactive analyze flow to inject user-selected LLM
    options for that single run without persisting to the DB. Calling with
    an empty dict resets the override.
    """
    global _config_override
    if not config:
        _config_override = None
        return
    if _config_override is None:
        _config_override = {}
    _config_override.update(config)


def get_config() -> Dict:
    """Return the runtime config dict, fresh from the DB on each call.

    Layers:
      1. DB-backed ``app_config`` rows (via config_service.load_config()).
      2. Env-var overrides for paths and secrets (handled inside load_config).
      3. Per-process override dict from ``set_config(...)`` (CLI single-run).

    Falls back to the static ``DEFAULT_CONFIG`` if the DB read fails (e.g.
    test-time module imports before schema setup).
    """
    try:
        # Local import to avoid a top-level cycle: web.config_service imports
        # from this package transitively in some test paths.
        from tradingagents.web.config_service import load_config
        cfg = load_config()
    except Exception:
        cfg = dict(default_config.DEFAULT_CONFIG)

    if _config_override:
        cfg.update(_config_override)
    return cfg
