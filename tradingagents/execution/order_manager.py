"""Order management for simulated intraday trades."""

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional, List, Dict


class OrderStatus(str, Enum):
    PENDING = "pending"
    FILLED = "filled"
    PARTIAL = "partial"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


@dataclass
class Order:
    ticker: str
    side: str  # "buy" or "sell"
    quantity: int
    entry_zone_low: float
    entry_zone_high: float
    stop_loss: float
    target_1: float
    target_2: float
    confidence_score: int = 5
    position_size_pct: float = 20.0
    skip_rule_time: Optional[str] = None  # HH:MM, e.g. "11:30"
    order_id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    status: OrderStatus = OrderStatus.PENDING
    filled_price: Optional[float] = None
    filled_qty: int = 0
    created_at: datetime = field(default_factory=datetime.now)
    filled_at: Optional[datetime] = None
    notes: str = ""


class OrderManager:
    """Manages pending and active orders."""

    def __init__(self):
        self.orders: Dict[str, Order] = {}

    def place_order(self, order: Order) -> str:
        self.orders[order.order_id] = order
        return order.order_id

    def cancel_order(self, order_id: str) -> bool:
        if order_id in self.orders:
            self.orders[order_id].status = OrderStatus.CANCELLED
            return True
        return False

    def check_entry(self, order_id: str, current_price: float, current_time: datetime) -> bool:
        """Check if current price is within entry zone. If so, fill the order."""
        order = self.orders.get(order_id)
        if not order or order.status not in (OrderStatus.PENDING, OrderStatus.PARTIAL):
            return False

        # Check skip rule
        if order.skip_rule_time:
            skip_hour, skip_min = map(int, order.skip_rule_time.split(":"))
            skip_dt = current_time.replace(hour=skip_hour, minute=skip_min, second=0, microsecond=0)
            if current_time > skip_dt:
                order.status = OrderStatus.EXPIRED
                order.notes = f"Skipped: not filled by {order.skip_rule_time}"
                return False

        if order.entry_zone_low <= current_price <= order.entry_zone_high:
            order.filled_price = current_price
            order.filled_qty = order.quantity
            order.status = OrderStatus.FILLED
            order.filled_at = current_time
            return True
        return False

    def check_exit(self, order_id: str, current_price: float, current_time: datetime) -> Optional[str]:
        """Check if SL or target hit. Returns 'sl', 'target1', 'target2', 'hard_exit', or None."""
        order = self.orders.get(order_id)
        if not order or order.status != OrderStatus.FILLED:
            return None

        if order.side != "buy":
            return None

        ep = order.filled_price
        if ep is None:
            return None

        # Stop loss
        if current_price <= order.stop_loss:
            return "sl"

        # Target 2 (check before T1 so we get the highest target)
        if current_price >= order.target_2:
            return "target2"

        # Target 1
        if current_price >= order.target_1:
            return "target1"

        return None

    def get_open_orders(self) -> List[Order]:
        return [o for o in self.orders.values() if o.status in (OrderStatus.PENDING, OrderStatus.PARTIAL)]

    def get_filled_orders(self) -> List[Order]:
        return [o for o in self.orders.values() if o.status == OrderStatus.FILLED]

    def to_dict(self) -> List[dict]:
        return [
            {
                "order_id": o.order_id,
                "ticker": o.ticker,
                "side": o.side,
                "quantity": o.quantity,
                "entry_zone_low": o.entry_zone_low,
                "entry_zone_high": o.entry_zone_high,
                "stop_loss": o.stop_loss,
                "target_1": o.target_1,
                "target_2": o.target_2,
                "status": o.status.value,
                "filled_price": o.filled_price,
                "filled_qty": o.filled_qty,
                "created_at": o.created_at.isoformat(),
                "filled_at": o.filled_at.isoformat() if o.filled_at else None,
                "notes": o.notes,
            }
            for o in self.orders.values()
        ]
