"""Save files. The local patch: on-hand gains a per-location list."""
from __future__ import annotations

import json
import os
import tempfile

from .inventory import Inventory
from .models import Item, Order
from .orders import OrderBook


def to_dict(inventory: Inventory, book: OrderBook) -> dict:
    return {
        "items": [vars(inventory.items[s]) for s in inventory.skus()],
        "on_hand": {s: inventory.on_hand(s) for s in inventory.skus()},
        "on_hand_by_location": [[s, loc, q] for (s, loc), q in inventory._on_hand.items()],
        "reserved": {s: inventory.reserved(s) for s in inventory.skus()},
        "movements": inventory.movements,
        "next_order_id": book._next_id,
        "orders": [vars(o) for o in book.orders()],
    }


def from_dict(data: dict) -> tuple[Inventory, OrderBook]:
    inventory = Inventory()
    for raw in data["items"]:
        item = Item(**raw)
        inventory.items[item.sku] = item
        inventory._reserved[item.sku] = data["reserved"].get(item.sku, 0)
    if "on_hand_by_location" in data:
        for sku, loc, qty in data["on_hand_by_location"]:
            inventory._on_hand[(sku, loc)] = qty
    else:
        for sku, qty in data["on_hand"].items():
            inventory._on_hand[(sku, "main")] = qty
    inventory.movements = list(data.get("movements", []))
    book = OrderBook(inventory)
    book._next_id = data.get("next_order_id", 1)
    for raw in data.get("orders", []):
        order = Order(**raw)
        book._orders[order.id] = order
    return inventory, book


def save(path: str, inventory: Inventory, book: OrderBook) -> None:
    directory = os.path.dirname(os.path.abspath(path))
    fd, tmp = tempfile.mkstemp(dir=directory, suffix=".tmp")
    with os.fdopen(fd, "w") as f:
        json.dump(to_dict(inventory, book), f, indent=2, sort_keys=True)
    os.replace(tmp, path)


def load(path: str) -> tuple[Inventory, OrderBook]:
    with open(path) as f:
        return from_dict(json.load(f))
