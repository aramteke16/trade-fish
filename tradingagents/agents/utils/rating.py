"""Shared 2-tier decision vocabulary and a deterministic heuristic parser.

The same Buy/Skip vocabulary is used by:
- The Research Manager (intraday entry plan recommendation)
- The Trader (transaction proposal action)
- The Portfolio Manager (final intraday decision)
- The signal processor (decision extracted for downstream consumers)
- The memory log (decision tag stored alongside each entry)

Centralising it here avoids drift between those call sites.
"""

from __future__ import annotations

import re
from typing import Tuple


# Canonical 2-tier scale for same-day intraday long-only trading.
RATINGS_2_TIER: Tuple[str, ...] = ("Buy", "Skip")

_RATING_SET = {r.lower() for r in RATINGS_2_TIER}

# Matches "Rating: X" / "rating - X" / "Rating: **X**" — tolerates markdown
# bold wrappers and either a colon or hyphen separator.
_RATING_LABEL_RE = re.compile(r"rating.*?[:\-][\s*]*(\w+)", re.IGNORECASE)


def parse_rating(text: str, default: str = "Skip") -> str:
    """Heuristically extract a Buy/Skip decision from prose text.

    Two-pass strategy:
    1. Look for an explicit "Rating: X" label (tolerant of markdown bold).
    2. Fall back to the first 2-tier rating word found anywhere in the text.

    Returns ``"Buy"`` or ``"Skip"`` (Title-cased), or ``default`` if no
    rating word appears. Default is ``"Skip"`` — the safe choice when in doubt.
    """
    for line in text.splitlines():
        m = _RATING_LABEL_RE.search(line)
        if m and m.group(1).lower() in _RATING_SET:
            return m.group(1).capitalize()

    for line in text.splitlines():
        for word in line.lower().split():
            clean = word.strip("*:.,")
            if clean in _RATING_SET:
                return clean.capitalize()

    return default
