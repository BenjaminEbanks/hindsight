"""What the ops team looks at every morning."""
from __future__ import annotations

from .inventory import Inventory
from .orders import OrderBook


def stock_report(inventory: Inventory) -> list[dict]:
    """One row per SKU."""
    return [{"sku": s,
             "name": inventory.item(s).name,
             "on_hand": inventory.on_hand(s),
             "reserved": inventory.reserved(s),
             "available": inventory.available(s)}
            for s in inventory.skus()]


def low_stock(inventory: Inventory) -> list[str]:
    """SKUs whose available units are at or below their reorder point."""
    return [s for s in inventory.skus()
            if inventory.available(s) <= inventory.item(s).reorder_point]


def stock_value_cents(inventory: Inventory) -> int:
    return sum(inventory.on_hand(s) * inventory.item(s).unit_cost_cents
               for s in inventory.skus())


def reorder_suggestions(inventory: Inventory, cover: int = 2) -> dict[str, int]:
    """How many to buy to reach `cover` times the reorder point."""
    out = {}
    for sku in low_stock(inventory):
        target = inventory.item(sku).reorder_point * cover
        needed = target - inventory.available(sku)
        if needed > 0:
            out[sku] = needed
    return out


def order_backlog(book: OrderBook) -> dict[str, int]:
    """Units promised to open orders, per SKU."""
    backlog: dict[str, int] = {}
    for order in book.open_orders():
        for sku, qty in order.lines.items():
            backlog[sku] = backlog.get(sku, 0) + qty
    return backlog


def customer_summary(book: OrderBook) -> dict[str, dict[str, int]]:
    out: dict[str, dict[str, int]] = {}
    for order in book.orders():
        row = out.setdefault(order.customer,
                             {"open": 0, "shipped": 0, "cancelled": 0})
        row[order.status] += 1
    return out
