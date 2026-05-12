"""Position tracking and capital management."""

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, List, Dict

from .charges import round_trip_charges

logger = logging.getLogger(__name__)


@dataclass
class Position:
    ticker: str
    quantity: int
    entry_price: float
    stop_loss: float
    target_1: float
    target_2: float
    order_id: str
    partial_exit_1_done: bool = False
    partial_exit_2_done: bool = False
    opened_at: datetime = field(default_factory=datetime.now)
    closed_at: Optional[datetime] = None
    exit_price: Optional[float] = None
    pnl: Optional[float] = None
    pnl_pct: Optional[float] = None
    exit_reason: Optional[str] = None


class PositionTracker:
    """Tracks open positions, closed trades, and capital."""

    def __init__(self, initial_capital: float = 20000.0):
        self.initial_capital = initial_capital
        self.capital = initial_capital
        self.open_positions: Dict[str, Position] = {}
        self.closed_trades: List[Position] = []
        self.daily_pnl = 0.0
        self.weekly_pnl = 0.0
        self.daily_loss = 0.0

    def add_position(self, position: Position, capital_used: float):
        if capital_used > self.capital:
            logger.warning(
                "add_position %s: capital_used ₹%.2f > available ₹%.2f — capital will go negative",
                position.ticker, capital_used, self.capital,
            )
        self.capital -= capital_used
        self.open_positions[position.ticker] = position
        logger.info("Opened position %s qty=%d @ %.2f", position.ticker, position.quantity, position.entry_price)

    def close_position(self, ticker: str, exit_price: float, exit_reason: str, current_time: datetime):
        """Fully close an open position. Books realized P&L net of Zerodha
        intraday charges and credits the position's locked capital + P&L
        back to free cash."""
        pos = self.open_positions.pop(ticker, None)
        if not pos:
            return None
        return self._book_close(pos, pos.quantity, exit_price, exit_reason, current_time, fully_closed=True)

    def partial_close_position(
        self,
        ticker: str,
        exit_price: float,
        exit_qty: int,
        exit_reason: str,
        current_time: datetime,
    ):
        """Close ``exit_qty`` shares of an open position at ``exit_price``.

        Books realized P&L on the exited shares, credits their locked capital
        back to free cash, and reduces the in-memory position by ``exit_qty``.
        The remaining open position keeps its targets unchanged but its SL is
        moved to break-even (standard intraday risk-off-runner pattern).

        Returns a copy-Position representing only the exited slice (with its
        own pnl/pnl_pct), suitable for logging and DB-recording.
        """
        pos = self.open_positions.get(ticker)
        if not pos or exit_qty <= 0 or exit_qty > pos.quantity:
            return None

        # Slice off exit_qty into a synthetic Position so charge math and
        # closed-trades booking treat it like an independent fill.
        slice_pos = Position(
            ticker=pos.ticker,
            quantity=exit_qty,
            entry_price=pos.entry_price,
            stop_loss=pos.stop_loss,
            target_1=pos.target_1,
            target_2=pos.target_2,
            order_id=f"{pos.order_id}-p1",
            opened_at=pos.opened_at,
        )
        booked = self._book_close(slice_pos, exit_qty, exit_price, exit_reason, current_time, fully_closed=False)

        # Reduce the live position. Standard practice on intraday partial:
        # move SL to break-even so the runner is risk-free.
        pos.quantity -= exit_qty
        pos.stop_loss = pos.entry_price
        pos.partial_exit_1_done = True
        return booked

    def _book_close(
        self,
        pos: "Position",
        qty: int,
        exit_price: float,
        exit_reason: str,
        current_time: datetime,
        *,
        fully_closed: bool,
    ):
        """Shared accounting for full and partial closes.

        - Computes Zerodha-realistic round-trip charges on (entry, exit).
        - Books net P&L (gross − charges) onto the position record.
        - Credits the position's locked capital plus net P&L back to free cash.
        - Adds to closed_trades and updates daily_pnl / daily_loss.
        """
        pos.exit_price = exit_price
        pos.closed_at = current_time
        pos.exit_reason = exit_reason

        buy_value = pos.entry_price * qty
        sell_value = exit_price * qty
        gross_pnl = sell_value - buy_value
        charges = round_trip_charges(buy_value, sell_value)

        pos.pnl = gross_pnl - charges.total
        pos.pnl_pct = (pos.pnl / buy_value) * 100 if buy_value > 0 else 0

        # Free cash gets the locked capital back plus the net P&L.
        self.capital += buy_value + pos.pnl
        self.daily_pnl += pos.pnl
        if pos.pnl < 0:
            self.daily_loss += abs(pos.pnl)

        self.closed_trades.append(pos)
        logger.info(
            "%s %s qty=%d @ %.2f (entry=%.2f) reason=%s pnl=₹%.2f charges=₹%.2f",
            "Closed" if fully_closed else "Partial-exit",
            pos.ticker, qty, exit_price, pos.entry_price, exit_reason,
            pos.pnl, charges.total,
        )
        return pos

    def close_all_positions(self, prices: Dict[str, float], current_time: datetime, reason: str = "hard_exit"):
        closed = []
        for ticker in list(self.open_positions.keys()):
            price = prices.get(ticker)
            if price:
                pos = self.close_position(ticker, price, reason, current_time)
                if pos:
                    closed.append(pos)
        return closed

    def get_open_pnl(self, current_prices: Dict[str, float]) -> float:
        total = 0.0
        for ticker, pos in self.open_positions.items():
            price = current_prices.get(ticker)
            if price:
                total += (price - pos.entry_price) * pos.quantity
        return total

    def get_metrics(self, current_prices: Optional[Dict[str, float]] = None) -> dict:
        """Compute portfolio metrics.

        ``current_prices`` lets the caller mark open positions to market so
        ``current_capital`` and ``total_return_pct`` reflect *total portfolio
        value*, not just free cash. Without prices, open positions are valued
        at their entry price (i.e., assumed flat — a conservative default).
        """
        wins = [t for t in self.closed_trades if t.pnl and t.pnl > 0]
        losses = [t for t in self.closed_trades if t.pnl and t.pnl <= 0]
        total_pnl = sum(t.pnl for t in self.closed_trades if t.pnl)
        win_rate = len(wins) / len(self.closed_trades) * 100 if self.closed_trades else 0
        avg_win = sum(t.pnl for t in wins) / len(wins) if wins else 0
        avg_loss = sum(t.pnl for t in losses) / len(losses) if losses else 0

        # Mark-to-market: free cash + value of open positions at current price.
        prices = current_prices or {}
        open_value = 0.0
        unrealized_pnl = 0.0
        for ticker, pos in self.open_positions.items():
            mark = prices.get(ticker, pos.entry_price)
            open_value += mark * pos.quantity
            unrealized_pnl += (mark - pos.entry_price) * pos.quantity
        portfolio_value = self.capital + open_value

        # Max drawdown computed on closed-trade equity curve (intraday-conservative).
        peak = self.initial_capital
        trough = self.initial_capital
        running = self.initial_capital
        for t in self.closed_trades:
            if t.pnl:
                running += t.pnl
                if running > peak:
                    peak = running
                if running < trough:
                    trough = running
        max_dd = ((peak - trough) / peak) * 100 if peak > 0 else 0

        return {
            "initial_capital": self.initial_capital,
            "current_capital": portfolio_value,    # cash + mark-to-market open positions
            "free_cash": self.capital,             # raw cash bucket only
            "open_positions_value": open_value,
            "unrealized_pnl": unrealized_pnl,
            "total_pnl": total_pnl,                # realized only
            "total_return_pct": ((portfolio_value - self.initial_capital) / self.initial_capital) * 100,
            "win_rate": win_rate,
            "total_trades": len(self.closed_trades),
            "winning_trades": len(wins),
            "losing_trades": len(losses),
            "avg_win": avg_win,
            "avg_loss": avg_loss,
            "max_drawdown_pct": max_dd,
            "daily_pnl": self.daily_pnl,
            "daily_loss": self.daily_loss,
            "open_positions_count": len(self.open_positions),
        }

    def reset_daily(self):
        self.daily_pnl = 0.0
        self.daily_loss = 0.0

    def reset_weekly(self):
        self.weekly_pnl = 0.0
