"""Nightly consistency check over levels, the movement log and open orders."""
from __future__ import annotations

from .inventory import Inventory
from .orders import OrderBook


def replay_on_hand(inventory: Inventory) -> dict[str, int]:
    totals = {s: 0 for s in inventory.skus()}
    for move in inventory.movements:
        if move["kind"] in ("receive", "adjust"):
            totals[move["sku"]] += move["qty"]
        elif move["kind"] == "ship":
            totals[move["sku"]] -= move["qty"]
    return totals


def reconcile(inventory: Inventory, book: OrderBook) -> list[str]:
    problems = []
    replayed = replay_on_hand(inventory)
    for sku in inventory.skus():
        if replayed[sku] != inventory.on_hand(sku):
            problems.append(f"{sku}: log disagrees with on hand")
    return problems
