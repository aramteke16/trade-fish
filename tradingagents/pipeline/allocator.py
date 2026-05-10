"""Cross-stock ranking and capital allocator (Phase 2.5).

Takes the per-stock plans produced by the multi-agent graph and decides:
  1. Which K stocks to actually trade today.
  2. How much capital to put on each (position_size_pct).
  3. What to do if every stock came back Skip (force-best-of-N rule).

Design notes — based on standard quant-allocation practice:

- **Top-K selection**: Pattern from Microsoft Qlib's ``TopkDropoutStrategy`` —
  rank a scored set of candidates, take the top K. Equal-weight inside top-K
  is the simple version; we use a tilt instead.

- **Confidence-tilted weighting**: Modern Portfolio Theory says higher
  expected return earns more capital. We compute a per-stock ``rank_score =
  confidence × reward_to_risk`` (analogue of Kelly's f* = edge / odds), then
  weight allocations within top-K proportional to that score.

- **Half-Kelly / deploy_pct cap**: Full-Kelly maximises log-growth but is too
  aggressive for real-world model error. We cap total deployed capital to
  ``deploy_pct`` (default 70%) — leaves 30% dry powder for the next day's
  drawdown buffer.

- **Per-stock cap**: ``max_capital_per_stock_pct`` (default 25%) prevents
  any single stock from getting too large a slice — diversification floor.

- **Force best-of-N**: We are an intraday-only desk. Sitting out an entire
  day produces zero return; the user wants ~1% daily target. So if every
  analyzed stock came back Skip but at least one had parseable levels, we
  promote the highest-confidence Skip to Buy at half its computed size.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import List, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def reward_to_risk(plan: dict) -> Optional[float]:
    """Compute R:R from entry to T1 using the upper end of the entry zone.

    R:R = (T1 - entry_zone_high) / (entry_zone_high - stop_loss)

    Returns None if any required level is missing or risk is zero/negative
    (entry below SL, which would be invalid).
    """
    entry_high = plan.get("entry_zone_high")
    stop_loss = plan.get("stop_loss")
    target_1 = plan.get("target_1")
    if not (entry_high and stop_loss and target_1):
        return None
    risk = entry_high - stop_loss
    reward = target_1 - entry_high
    if risk <= 0 or reward <= 0:
        return None
    return reward / risk


def has_valid_levels(plan: dict) -> bool:
    """A plan can be sized only if entry, SL, and T1 are all present."""
    return bool(
        plan.get("entry_zone_low")
        and plan.get("entry_zone_high")
        and plan.get("stop_loss")
        and plan.get("target_1")
    )


def _confidence(plan: dict) -> int:
    """Defensive read of confidence_score (1-10)."""
    return int(plan.get("confidence_score") or 0)


def _rank_score(plan: dict) -> Optional[float]:
    """confidence × R:R — higher is better. None if either input is missing."""
    rr = reward_to_risk(plan)
    conf = _confidence(plan)
    if rr is None or conf <= 0:
        return None
    return conf * rr


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


@dataclass
class AllocationResult:
    """Output of the allocator: traded plans + non-traded saves + diagnostics."""

    traded: List[dict]            # top-K plans, each with position_size_pct + rank_position
    saved_only: List[dict]        # plans with rating Buy that didn't make top-K (DB only)
    promoted_from_skip: bool      # True if force_best_of_n had to promote a Skip
    summary: str                  # human-readable one-liner for logs


def force_best_of_n(plans: List[dict]) -> List[dict]:
    """If every plan is Skip, promote the highest-confidence Skip with valid
    levels to Buy at *half* its computed size.

    Returns a new list (does not mutate the input). The promoted plan has its
    ``rating`` flipped to ``"Buy"`` and gains a ``promoted_from_skip=True``
    marker so downstream logging can flag it.
    """
    if not plans:
        return plans

    if any(p.get("rating") == "Buy" for p in plans):
        return plans  # at least one real Buy — nothing to promote

    candidates = [p for p in plans if has_valid_levels(p)]
    if not candidates:
        logger.info(
            "force_best_of_n: every plan is Skip and none have parseable levels — no trade today."
        )
        return plans

    # Pick the highest-confidence Skip among those with valid levels.
    best = max(candidates, key=_confidence)
    promoted = dict(best)  # shallow copy; the original stays a Skip in the list
    promoted["rating"] = "Buy"
    promoted["promoted_from_skip"] = True
    logger.info(
        "force_best_of_n: promoting %s (conf=%d, R:R=%.2f) from Skip to half-size Buy.",
        promoted.get("ticker"),
        _confidence(promoted),
        reward_to_risk(promoted) or 0.0,
    )

    # Replace the original best entry with the promoted one; keep order.
    out: List[dict] = []
    for p in plans:
        if p is best:
            out.append(promoted)
        else:
            out.append(p)
    return out


def rank_and_allocate(
    plans: List[dict],
    *,
    top_k: int = 3,
    deploy_pct: float = 70.0,
    max_per_stock_pct: float = 25.0,
    half_size_promoted: bool = True,
) -> AllocationResult:
    """Rank Buy plans and assign position_size_pct to the top K.

    Args:
      plans: All analyzed plans from Phase 2 (some Buy, some Skip). Each plan
        is the dict produced by ``extract_trade_plan()`` — it must already
        contain ``ticker``, ``rating``, ``confidence_score``, and the price
        levels (entry_zone_low/high, stop_loss, target_1).
      top_k: How many positions to actually trade. Default 3.
      deploy_pct: Total capital % to deploy across the top K. Default 70 —
        leaves 30 % dry powder. This is the half-Kelly-style fractional cap.
      max_per_stock_pct: Hard cap per stock. Default 25 — diversification floor.
      half_size_promoted: If a plan was promoted from Skip via
        ``force_best_of_n``, halve its allocated size to reflect the lower
        conviction. Default True.

    Returns AllocationResult with the top-K plans (each annotated with
    ``position_size_pct`` and ``rank_position``) plus the remaining Buys
    saved-but-not-traded.

    Algorithm:
      1. Run force_best_of_n so we always have at least one Buy if possible.
      2. Filter to Buys with parseable levels and a positive R:R.
      3. Compute rank_score = confidence × R:R for each.
      4. Sort by rank_score desc; take top K.
      5. weights = rank_score / sum(top-K rank_score)
      6. raw_size = weights × deploy_pct
      7. Apply max_per_stock_pct cap. If any plan was clipped, redistribute
         the leftover proportionally to non-clipped plans (so we don't lose
         capital from clipping).
      8. If a plan was promoted from Skip, halve its size.
      9. Mutate each plan dict to include position_size_pct and rank_position.
    """
    if not plans:
        return AllocationResult(traded=[], saved_only=[], promoted_from_skip=False,
                                summary="no plans to allocate")

    plans = force_best_of_n(plans)
    promoted = any(p.get("promoted_from_skip") for p in plans)

    # Eligible = Buy + valid levels + computable R:R.
    eligible: List[dict] = []
    for p in plans:
        if p.get("rating") != "Buy":
            continue
        if not has_valid_levels(p):
            continue
        if _rank_score(p) is None:
            continue
        eligible.append(p)

    if not eligible:
        return AllocationResult(
            traded=[], saved_only=[], promoted_from_skip=promoted,
            summary="no Buy plan had complete levels — nothing tradeable today",
        )

    # Sort by rank_score desc.
    eligible.sort(key=lambda p: _rank_score(p) or 0.0, reverse=True)
    top = eligible[:top_k]
    rest = eligible[top_k:]

    # Tilt weights by rank_score (Kelly-edge analogue).
    scores = [_rank_score(p) or 0.0 for p in top]
    total = sum(scores) or 1.0
    weights = [s / total for s in scores]
    raw_sizes = [w * deploy_pct for w in weights]

    # Apply per-stock cap and redistribute clipped excess.
    sizes = _redistribute_after_cap(raw_sizes, cap=max_per_stock_pct, total=deploy_pct)

    # Apply half-size for promoted-from-Skip plans.
    if half_size_promoted:
        sizes = [
            sz / 2.0 if top[i].get("promoted_from_skip") else sz
            for i, sz in enumerate(sizes)
        ]

    # Annotate plans in-place so PaperTrader picks them up.
    for i, (plan, size) in enumerate(zip(top, sizes), start=1):
        plan["position_size_pct"] = round(size, 2)
        plan["rank_position"] = i
        plan["rank_score"] = round(_rank_score(plan) or 0.0, 3)

    # Tag rest so the CLI can show "ranked #N — saved only".
    for j, plan in enumerate(rest, start=top_k + 1):
        plan["position_size_pct"] = 0
        plan["rank_position"] = j
        plan["rank_score"] = round(_rank_score(plan) or 0.0, 3)

    summary = (
        f"top-{len(top)} of {len(eligible)} eligible: "
        + ", ".join(
            f"#{i + 1} {p['ticker']} (size={p['position_size_pct']:.1f}%, "
            f"score={p['rank_score']:.2f})"
            for i, p in enumerate(top)
        )
    )

    return AllocationResult(
        traded=top,
        saved_only=rest,
        promoted_from_skip=promoted,
        summary=summary,
    )


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _redistribute_after_cap(raw_sizes: List[float], *, cap: float, total: float) -> List[float]:
    """Apply per-stock cap and redistribute clipped excess to remaining slots.

    Water-filling algorithm: in each pass, lock any slot that's at or above the
    cap (clamp it to cap and freeze it), then proportionally redistribute the
    leftover budget across the still-unfrozen slots according to their current
    weights. Repeat until no more clipping occurs. Always converges in at most
    N iterations because each iteration freezes at least one slot.

    The total deployed equals min(total, cap * N) — capped above by the budget,
    capped below only if every slot is at the cap.
    """
    n = len(raw_sizes)
    if n == 0:
        return []
    target = min(total, cap * n)
    final = [0.0] * n
    locked = [False] * n
    # Start with the proportional shares from raw_sizes.
    weights = [max(s, 0.0) for s in raw_sizes]
    remaining_budget = target

    for _ in range(n + 1):  # at most N freezes possible
        active = [i for i in range(n) if not locked[i]]
        if not active:
            break
        active_weight_sum = sum(weights[i] for i in active)
        if active_weight_sum <= 0:
            # No weight left to distribute — split remaining equally among active.
            equal = remaining_budget / len(active)
            for i in active:
                final[i] = min(equal, cap)
            break

        any_clipped = False
        for i in active:
            share = remaining_budget * (weights[i] / active_weight_sum)
            if share >= cap - 1e-9:
                final[i] = cap
                locked[i] = True
                remaining_budget -= cap
                any_clipped = True
            else:
                final[i] = share
        if not any_clipped:
            break

    return final
