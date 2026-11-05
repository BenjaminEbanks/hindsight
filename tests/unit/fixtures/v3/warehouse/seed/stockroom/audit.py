"""Nightly consistency check over levels, the movement log and open orders."""
from __future__ import annotations

from .inventory import Inventory
from .orders import OrderBook


def replay_on_hand(inventory: Inventory) -> dict[str, int]:
    """On-hand per SKU as implied by the movement log."""
    totals = {s: 0 for s in inventory.skus()}
    for move in inventory.movements:
        if move["kind"] == "receive":
            totals[move["sku"]] += move["qty"]
        elif move["kind"] == "ship":
            totals[move["sku"]] -= move["qty"]
    return totals


def reconcile(inventory: Inventory, book: OrderBook) -> list[str]:
    """Human-readable problems; an empty list means everything agrees."""
    problems = []
    replayed = replay_on_hand(inventory)
    promised: dict[str, int] = {}
    for order in book.open_orders():
        for sku, qty in order.lines.items():
            promised[sku] = promised.get(sku, 0) + qty
    for sku in inventory.skus():
        if replayed[sku] != inventory.on_hand(sku):
            problems.append(f"{sku}: log says {replayed[sku]}, "
                            f"on hand is {inventory.on_hand(sku)}")
        if promised.get(sku, 0) != inventory.reserved(sku):
            problems.append(f"{sku}: orders promise {promised.get(sku, 0)}, "
                            f"reserved is {inventory.reserved(sku)}")
        if inventory.available(sku) < 0:
            problems.append(f"{sku}: available is negative")
    return problems
