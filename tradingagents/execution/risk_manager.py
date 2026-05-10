"""Mid-day trailing-stop / breakeven risk manager.

Runs every poll cycle inside :class:`MarketMonitor`. Inspects open positions
and current market prices, then raises stop-loss levels when a trade has moved
into profit. The exit machinery in :class:`OrderManager.check_exit` already
honors the live SL on every tick, so simply mutating ``Position.stop_loss``
(and the underlying ``Order.stop_loss``) is enough — no new exit path needed.

Two-tier ladder (defaults; overridable from default_config.py):

  - At >= ``breakeven_trigger_pct`` unrealized profit: raise SL to entry price.
    The trade can no longer lose money (modulo charges).

  - At >= ``trail_trigger_pct``: raise SL to ``entry × (1 + trail_lock_pct/100)``.
    Locks in a small profit floor even if the position retraces.

Ratchet: SL is only ever raised, never lowered. Once a position has armed the
trailing stop, a pullback below the trigger does not relax it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Dict, Iterable, List

logger = logging.getLogger(__name__)


# Defaults; the caller can override via apply_trailing_stops(thresholds=…).
DEFAULT_BREAKEVEN_TRIGGER_PCT = 0.5
DEFAULT_TRAIL_TRIGGER_PCT = 1.0
DEFAULT_TRAIL_LOCK_PCT = 0.3


@dataclass
class RiskThresholds:
    """Tunable trailing-stop ladder parameters (all in % of entry price)."""

    breakeven_trigger_pct: float = DEFAULT_BREAKEVEN_TRIGGER_PCT
    trail_trigger_pct: float = DEFAULT_TRAIL_TRIGGER_PCT
    trail_lock_pct: float = DEFAULT_TRAIL_LOCK_PCT


@dataclass
class RiskAction:
    """A single SL adjustment, returned for logging / display."""

    ticker: str
    old_sl: float
    new_sl: float
    reason: str  # "breakeven" or "trail_<lock_pct>pct"
    unrealized_pct: float


def apply_trailing_stops(
    open_positions: Dict[str, "Position"],  # PositionTracker.open_positions
    all_orders: Iterable["Order"],          # ALL orders (filled + pending) — see note
    current_prices: Dict[str, float],
    thresholds: RiskThresholds = None,
) -> List[RiskAction]:
    """Mutate ``Position.stop_loss`` and the parent ``Order.stop_loss`` per the
    trailing ladder. Returns the list of actions taken.

    The exit-check path (``OrderManager.check_exit``) reads ``order.stop_loss``
    on every tick — including for FILLED orders. So we must sync the parent
    Order's SL here too, otherwise the raised position SL is invisible to the
    exit machinery.

    Pass ``OrderManager.orders.values()`` (every order regardless of status),
    not ``get_open_orders()`` (which excludes filled ones).
    """
    th = thresholds or RiskThresholds()
    actions: List[RiskAction] = []

    # Build an order lookup by order_id for O(1) sync.
    order_by_id = {o.order_id: o for o in all_orders}

    for ticker, pos in open_positions.items():
        price = current_prices.get(ticker)
        if not price or price <= 0:
            continue

        entry = pos.entry_price
        if entry <= 0:
            continue
        unrealized_pct = ((price - entry) / entry) * 100.0

        # Decide the new SL based on the ladder. Higher tier wins.
        if unrealized_pct >= th.trail_trigger_pct:
            desired_sl = entry * (1 + th.trail_lock_pct / 100.0)
            reason = f"trail_{th.trail_lock_pct}pct"
        elif unrealized_pct >= th.breakeven_trigger_pct:
            desired_sl = entry
            reason = "breakeven"
        else:
            continue  # not in profit far enough

        # One-way ratchet — only raise, never lower.
        if desired_sl <= pos.stop_loss:
            continue

        old_sl = pos.stop_loss
        pos.stop_loss = desired_sl

        # Sync the parent Order so OrderManager.check_exit (which reads order.stop_loss)
        # sees the same value. Partial-fill edge case: the post-T1 runner is a different
        # Position object whose order_id has a "-p1" suffix; the original Order is fine.
        order = order_by_id.get(pos.order_id)
        if order is not None:
            order.stop_loss = desired_sl

        actions.append(RiskAction(
            ticker=ticker,
            old_sl=round(old_sl, 4),
            new_sl=round(desired_sl, 4),
            reason=reason,
            unrealized_pct=round(unrealized_pct, 3),
        ))

    if actions:
        logger.info("Trailing-stop ladder applied: %d SL raises", len(actions))
    return actions
