"""Customer orders. Placing an order reserves exact stock, all lines or none."""
from __future__ import annotations

from .inventory import InsufficientStock, Inventory
from .models import Order


class OrderBook:
    def __init__(self, inventory: Inventory) -> None:
        self.inventory = inventory
        self._orders: dict[int, Order] = {}
        self._next_id = 1

    def place(self, customer: str, lines: dict[str, int],
              location: str | None = None, as_of: str | None = None) -> Order:
        if not lines:
            raise ValueError("an order needs at least one line")
        for sku, qty in lines.items():
            have = self.inventory.available(sku, location, as_of)
            if qty > have:
                raise InsufficientStock(f"{sku}: wanted {qty}, have {have}")
        order = Order(self._next_id, customer, dict(lines))
        self._next_id += 1
        for sku, qty in lines.items():
            order.picks += self.inventory.allocate(
                sku, qty, location, as_of, ref=f"order:{order.id}")
        self._orders[order.id] = order
        return order

    def get(self, order_id: int) -> Order:
        return self._orders[order_id]

    def picks(self, order_id: int) -> list[dict]:
        return [dict(p) for p in self.get(order_id).picks]

    def orders(self) -> list[Order]:
        return [self._orders[k] for k in sorted(self._orders)]

    def open_orders(self) -> list[Order]:
        return [o for o in self.orders() if o.status == "open"]

    def cancel(self, order_id: int) -> None:
        order = self._require_open(order_id)
        self.inventory.release_picks(order.picks, ref=f"order:{order.id}")
        order.status = "cancelled"

    def ship(self, order_id: int) -> None:
        order = self._require_open(order_id)
        self.inventory.ship_picks(order.picks, ref=f"order:{order.id}")
        order.status = "shipped"

    def _require_open(self, order_id: int) -> Order:
        order = self.get(order_id)
        if order.status != "open":
            raise ValueError(f"order {order_id} is {order.status}")
        return order
