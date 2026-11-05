"""Customer orders. Placing an order reserves stock, all lines or none."""
from __future__ import annotations

from .inventory import InsufficientStock, Inventory
from .models import Order


class OrderBook:
    def __init__(self, inventory: Inventory) -> None:
        self.inventory = inventory
        self._orders: dict[int, Order] = {}
        self._next_id = 1

    def place(self, customer: str, lines: dict[str, int]) -> Order:
        if not lines:
            raise ValueError("an order needs at least one line")
        for sku, qty in lines.items():
            if qty > self.inventory.available(sku):
                raise InsufficientStock(
                    f"{sku}: wanted {qty}, have {self.inventory.available(sku)}")
        order = Order(self._next_id, customer, dict(lines))
        self._next_id += 1
        for sku, qty in lines.items():
            self.inventory.reserve(sku, qty, ref=f"order:{order.id}")
        self._orders[order.id] = order
        return order

    def get(self, order_id: int) -> Order:
        return self._orders[order_id]

    def orders(self) -> list[Order]:
        return [self._orders[k] for k in sorted(self._orders)]

    def open_orders(self) -> list[Order]:
        return [o for o in self.orders() if o.status == "open"]

    def cancel(self, order_id: int) -> None:
        order = self._require_open(order_id)
        for sku, qty in order.lines.items():
            self.inventory.release(sku, qty, ref=f"order:{order.id}")
        order.status = "cancelled"

    def ship(self, order_id: int) -> None:
        order = self._require_open(order_id)
        for sku, qty in order.lines.items():
            self.inventory.ship(sku, qty, ref=f"order:{order.id}")
        order.status = "shipped"

    def _require_open(self, order_id: int) -> Order:
        order = self.get(order_id)
        if order.status != "open":
            raise ValueError(f"order {order_id} is {order.status}")
        return order
