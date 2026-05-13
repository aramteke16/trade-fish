"""Paper trading engine for intraday Indian equities."""

import logging
from datetime import datetime, time
from typing import List, Optional, Dict

from .order_manager import OrderManager, Order, OrderStatus
from .position_tracker import PositionTracker, Position

logger = logging.getLogger(__name__)


class PaperTrader:
    """Simulates intraday trades with realistic fills and risk rules."""

    def __init__(
        self,
        initial_capital: float = 20000.0,
        max_capital_per_stock_pct: float = 25.0,
        max_loss_per_trade_pct: float = 1.5,
        daily_loss_limit_pct: float = 3.0,
        weekly_loss_limit_pct: float = 5.0,
        hard_exit_time: str = "15:15",
    ):
        self.initial_capital = initial_capital
        self.max_capital_per_stock_pct = max_capital_per_stock_pct
        self.max_loss_per_trade_pct = max_loss_per_trade_pct
        self.daily_loss_limit_pct = daily_loss_limit_pct
        self.weekly_loss_limit_pct = weekly_loss_limit_pct
        self.hard_exit_hour, self.hard_exit_minute = map(int, hard_exit_time.split(":"))

        self.order_manager = OrderManager()
        self.position_tracker = PositionTracker(initial_capital)
        self.trading_paused = False
        self.pause_reason = ""
        # Latest rejection reason per ticker — populated when place_trade_plan
        # rejects an order. Lets the dispatcher / Telegram notifier explain
        # *why* an order wasn't placed without screen-scraping logs.
        self.last_rejection_reason: Dict[str, str] = {}

    def _pending_reserved_capital(self) -> float:
        """Sum of capital tied up by pending (unfilled) orders.

        Uses ``entry_zone_high`` as the conservative reservation per order
        (the buy-limit price). On fill, the actual capital deducted from
        free_cash may be lower; the difference is implicitly released because
        this method recounts pending orders fresh on each call.
        """
        from .order_manager import OrderStatus
        return sum(
            (o.entry_zone_high or 0.0) * (o.quantity or 0)
            for o in self.order_manager.orders.values()
            if o.status == OrderStatus.PENDING
        )

    def get_capital_state(self, current_prices: Optional[Dict[str, float]] = None) -> dict:
        """Live capital snapshot.

        Returns the buckets the project's capital model exposes to the UI:
        seed_capital (lifetime constant), start_capital (today's start),
        current_value (start + realized_pnl), free_cash (available for new
        orders, net of pending reservations), invested (in filled positions),
        pending_reserved, and realized_pnl.

        When ``current_prices`` is provided, also returns ``unrealized_pnl``
        (mark-to-market on open positions) and ``mtm_value`` (current_value +
        unrealized_pnl). Without prices, unrealized fields are 0.
        """
        invested = sum(
            (p.entry_price or 0.0) * (p.quantity or 0)
            for p in self.position_tracker.open_positions.values()
        )
        pending = self._pending_reserved_capital()
        start_capital = float(self.position_tracker.initial_capital)
        realized = float(self.position_tracker.daily_pnl)
        current_value = start_capital + realized
        free_cash = max(0.0, float(self.position_tracker.capital) - pending)

        unrealized = 0.0
        if current_prices:
            for ticker, pos in self.position_tracker.open_positions.items():
                price = current_prices.get(ticker)
                if price and price > 0:
                    unrealized += (price - (pos.entry_price or 0.0)) * (pos.quantity or 0)
        mtm_value = current_value + unrealized

        return {
            "start_capital": round(start_capital, 2),
            "current_value": round(current_value, 2),
            "free_cash": round(free_cash, 2),
            "invested": round(invested, 2),
            "pending_reserved": round(pending, 2),
            "realized_pnl": round(realized, 2),
            "unrealized_pnl": round(unrealized, 2),
            "mtm_value": round(mtm_value, 2),
            "open_positions_count": len(self.position_tracker.open_positions),
        }

    def _reject(self, ticker: str, reason: str) -> None:
        """Record why a plan was rejected, log it, and return None upstream."""
        self.last_rejection_reason[ticker] = reason
        logger.warning("[place_trade_plan] REJECT %s — %s", ticker, reason)

    def place_trade_plan(self, plan: dict) -> Optional[str]:
        """Place a trade plan from the portfolio manager.

        Returns the order_id on success, or None on rejection. The reason
        for any rejection is stored in ``self.last_rejection_reason[ticker]``
        so the dispatcher / Telegram notifier can report it verbatim.
        """
        ticker = plan["ticker"]

        if self.trading_paused:
            self._reject(ticker, f"trading paused — {self.pause_reason or 'unknown'}")
            return None

        # Prevent duplicate orders for the same ticker
        for o in self.order_manager.orders.values():
            if o.ticker == ticker and o.status in (OrderStatus.PENDING, OrderStatus.FILLED):
                self._reject(
                    ticker,
                    f"duplicate: order already exists (status={o.status.value})",
                )
                return None

        # Prevent ordering a ticker we already hold
        if ticker in self.position_tracker.open_positions:
            self._reject(ticker, "already holding an open position for this ticker")
            return None
        entry_low = plan.get("entry_zone_low")
        entry_high = plan.get("entry_zone_high")
        stop_loss = plan.get("stop_loss")
        target_1 = plan.get("target_1")
        target_2 = plan.get("target_2")
        position_size_pct = plan.get("position_size_pct", self.max_capital_per_stock_pct)
        confidence = plan.get("confidence_score", 5)
        from tradingagents.web.config_service import load_config as _lc
        _cfg = _lc()
        skip_rule = plan.get("skip_rule_time") or plan.get("skip_rule") or _cfg.get("execution_window_end")
        upper_band_only = bool(_cfg.get("use_upper_band_only", True))

        if upper_band_only:
            if not all([entry_high, stop_loss, target_1]):
                self._reject(ticker, "incomplete plan (need entry_zone_high, stop_loss, target_1)")
                return None
            if entry_low is None:
                entry_low = entry_high
        else:
            if not all([entry_high, stop_loss, target_1]) or entry_low is None:
                self._reject(ticker, "incomplete plan (need entry_zone_low/high, stop_loss, target_1)")
                return None

        # Capital allocation — only the cash that's not already committed by
        # earlier pending orders is usable for the next order. Without this
        # subtraction, placing multiple orders in a single phase would each
        # see the full free_cash and over-allocate at fill time.
        pending_reserved = self._pending_reserved_capital()
        available = max(0.0, self.position_tracker.capital - pending_reserved)
        max_capital = available * (position_size_pct / 100)
        max_capital = min(max_capital, available * (self.max_capital_per_stock_pct / 100))

        # Quantity based on max loss
        risk_per_share = entry_high - stop_loss
        if risk_per_share <= 0:
            self._reject(
                ticker,
                f"invalid risk: SL ₹{stop_loss} >= entry ₹{entry_high}",
            )
            return None

        max_loss_inr = available * (self.max_loss_per_trade_pct / 100)
        qty = int(max_loss_inr / risk_per_share)
        capital_needed = qty * entry_high

        if capital_needed > max_capital:
            qty = int(max_capital / entry_high)
            capital_needed = qty * entry_high

        if qty <= 0:
            self._reject(
                ticker,
                f"insufficient free cash — available ₹{available:.2f} "
                f"(free_cash ₹{self.position_tracker.capital:.2f}, "
                f"pending_reserved ₹{pending_reserved:.2f})",
            )
            return None

        if capital_needed > available:
            self._reject(
                ticker,
                f"capital_needed ₹{capital_needed:.2f} > available ₹{available:.2f}",
            )
            return None

        # Hard ceiling: total commitments (pending + invested + this order) must
        # not exceed initial capital. This catches edge cases where earlier
        # arithmetic drifts due to partial fills or rounding.
        invested = sum(
            (p.entry_price or 0.0) * (p.quantity or 0)
            for p in self.position_tracker.open_positions.values()
        )
        total_committed = pending_reserved + invested + capital_needed
        if total_committed > self.position_tracker.initial_capital:
            self._reject(
                ticker,
                f"would breach capital ceiling: pending ₹{pending_reserved:.0f} "
                f"+ invested ₹{invested:.0f} + this ₹{capital_needed:.0f} "
                f"= ₹{total_committed:.0f} > initial ₹{self.position_tracker.initial_capital:.0f}",
            )
            return None

        order = Order(
            ticker=ticker,
            side="buy",
            quantity=qty,
            entry_zone_low=entry_low,
            entry_zone_high=entry_high,
            stop_loss=stop_loss,
            target_1=target_1,
            target_2=target_2 or target_1 * 1.02,
            confidence_score=confidence,
            position_size_pct=position_size_pct,
            skip_rule_time=skip_rule,
        )

        oid = self.order_manager.place_order(order)
        logger.info("Placed order %s for %s qty=%d zone=%.2f-%.2f", oid, ticker, qty, entry_low, entry_high)
        return oid

    def on_price_tick(self, ticker: str, price: float, current_time: datetime):
        """Process a price tick for a ticker."""
        if self.trading_paused:
            return []

        # Check daily loss limit
        daily_loss_limit = self.position_tracker.initial_capital * (self.daily_loss_limit_pct / 100)
        if self.position_tracker.daily_loss >= daily_loss_limit:
            self.trading_paused = True
            self.pause_reason = "Daily loss limit hit"
            logger.warning("Daily loss limit hit. Pausing trading.")
            return []

        # Check hard exit (skipped in dry-run — dispatcher handles EOD explicitly)
        if current_time.time() >= time(self.hard_exit_hour, self.hard_exit_minute):
            try:
                from tradingagents.web.config_service import load_config
                if not load_config().get("dry_run_e2e", False):
                    return self._execute_hard_exit(ticker, price, current_time)
            except Exception:
                return self._execute_hard_exit(ticker, price, current_time)

        events = []

        # Check pending orders for this ticker
        for order in self.order_manager.get_open_orders():
            if order.ticker == ticker:
                filled = self.order_manager.check_entry(order.order_id, price, current_time)
                if filled:
                    pos = Position(
                        ticker=order.ticker,
                        quantity=order.quantity,
                        entry_price=order.filled_price,
                        stop_loss=order.stop_loss,
                        target_1=order.target_1,
                        target_2=order.target_2,
                        order_id=order.order_id,
                    )
                    capital_used = order.filled_price * order.quantity
                    self.position_tracker.add_position(pos, capital_used)
                    events.append({
                        "type": "entry", "ticker": ticker, "price": price, "qty": order.quantity,
                        "stop_loss": order.stop_loss, "target_1": order.target_1, "target_2": order.target_2,
                    })

        # Check open positions for this ticker
        if ticker in self.position_tracker.open_positions:
            pos = self.position_tracker.open_positions[ticker]
            exit_signal = self.order_manager.check_exit(pos.order_id, price, current_time)

            if exit_signal == "sl":
                closed = self.position_tracker.close_position(ticker, price, "stop_loss", current_time)
                events.append({"type": "exit", "ticker": ticker, "price": price, "reason": "stop_loss", "pnl": closed.pnl if closed else None, "pnl_pct": closed.pnl_pct if closed else None})

            elif exit_signal == "target1" and not pos.partial_exit_1_done:
                # Partial exit 50% (or full exit if qty < 2). When < 2 there is
                # nothing to split, so close the whole position at T1 instead
                # of holding for T2 with no half to ride — same risk profile.
                if pos.quantity < 2:
                    closed = self.position_tracker.close_position(
                        ticker, price, "target_1", current_time
                    )
                    events.append({
                        "type": "exit", "ticker": ticker, "price": price,
                        "reason": "target_1", "qty": closed.quantity if closed else 0,
                        "pnl": closed.pnl if closed else None,
                        "pnl_pct": closed.pnl_pct if closed else None,
                    })
                else:
                    exit_qty = pos.quantity // 2
                    booked = self.position_tracker.partial_close_position(
                        ticker, price, exit_qty, "target_1", current_time
                    )
                    if booked:
                        events.append({
                            "type": "partial_exit", "ticker": ticker, "price": price,
                            "reason": "target_1", "qty": exit_qty,
                            "pnl": booked.pnl, "pnl_pct": booked.pnl_pct,
                        })

            elif exit_signal == "target2":
                closed = self.position_tracker.close_position(ticker, price, "target_2", current_time)
                events.append({"type": "exit", "ticker": ticker, "price": price, "reason": "target_2", "pnl": closed.pnl if closed else None, "pnl_pct": closed.pnl_pct if closed else None})

        return events

    def _execute_hard_exit(self, ticker: str, price: float, current_time: datetime) -> List[dict]:
        events = []
        if ticker in self.position_tracker.open_positions:
            closed = self.position_tracker.close_position(ticker, price, "hard_exit", current_time)
            events.append({"type": "exit", "ticker": ticker, "price": price, "reason": "hard_exit", "pnl": closed.pnl if closed else None, "pnl_pct": closed.pnl_pct if closed else None})
        return events

    def force_exit_position(self, ticker: str, price: float, reason: str, current_time: datetime) -> List[dict]:
        """Force-close a single position by ticker."""
        if ticker not in self.position_tracker.open_positions:
            return []
        closed = self.position_tracker.close_position(ticker, price, reason, current_time)
        if closed is None:
            return []
        return [{"type": "exit", "ticker": closed.ticker, "price": closed.exit_price, "reason": reason, "pnl": closed.pnl, "pnl_pct": closed.pnl_pct}]

    def hard_exit_all(self, prices: Dict[str, float], current_time: datetime) -> List[dict]:
        """Close all open positions (e.g. at 3:15 PM)."""
        closed = self.position_tracker.close_all_positions(prices, current_time, "hard_exit")
        return [
            {"type": "exit", "ticker": c.ticker, "price": c.exit_price, "reason": "hard_exit", "pnl": c.pnl, "pnl_pct": c.pnl_pct}
            for c in closed
        ]

    def get_state(self) -> dict:
        return {
            "capital": self.position_tracker.capital,
            "initial_capital": self.initial_capital,
            "open_positions": [
                {
                    "ticker": p.ticker,
                    "quantity": p.quantity,
                    "entry_price": p.entry_price,
                    "stop_loss": p.stop_loss,
                    "target_1": p.target_1,
                    "target_2": p.target_2,
                    "partial_exit_1_done": p.partial_exit_1_done,
                }
                for p in self.position_tracker.open_positions.values()
            ],
            "orders": self.order_manager.to_dict(),
            "metrics": self.position_tracker.get_metrics(),
            "trading_paused": self.trading_paused,
            "pause_reason": self.pause_reason,
        }
