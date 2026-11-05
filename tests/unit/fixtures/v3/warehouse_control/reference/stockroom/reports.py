"""What the ops team looks at every morning."""
from __future__ import annotations

import csv
import io

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


def units_shipped_by_customer(book: OrderBook) -> dict[str, int]:
    """Units on shipped orders, per customer that has any."""
    out: dict[str, int] = {}
    for order in book.orders():
        if order.status == "shipped":
            out[order.customer] = out.get(order.customer, 0) + order.total_units()
    return out


def movement_totals(inventory: Inventory, sku: str) -> dict[str, int]:
    """Sum of logged quantities for one SKU, per movement kind that occurred."""
    inventory.item(sku)
    out: dict[str, int] = {}
    for move in inventory.movements:
        if move["sku"] == sku:
            out[move["kind"]] = out.get(move["kind"], 0) + move["qty"]
    return out


def stock_csv(inventory: Inventory) -> str:
    """The stock report as CSV text with a header row."""
    buffer = io.StringIO()
    fields = ["sku", "name", "on_hand", "reserved", "available"]
    writer = csv.DictWriter(buffer, fieldnames=fields, lineterminator="\n")
    writer.writeheader()
    writer.writerows(stock_report(inventory))
    return buffer.getvalue()
