"""Save files. Version 2 holds buckets; version-less files are the old format."""
from __future__ import annotations

import json
import os
import tempfile

from .inventory import DEFAULT_LOCATION, Inventory
from .models import Bucket, Item, Order
from .orders import OrderBook


def to_dict(inventory: Inventory, book: OrderBook) -> dict:
    return {
        "version": 2,
        "items": [vars(inventory.items[s]) for s in inventory.skus()],
        "buckets": [vars(b) for b in inventory.buckets.values()],
        "movements": inventory.movements,
        "next_order_id": book._next_id,
        "orders": [vars(o) for o in book.orders()],
    }


def from_dict(data: dict) -> tuple[Inventory, OrderBook]:
    inventory = Inventory()
    for raw in data["items"]:
        item = Item(**raw)
        inventory.items[item.sku] = item
    if data.get("version") == 2:
        for raw in data["buckets"]:
            bucket = Bucket(**raw)
            inventory.buckets[bucket.key] = bucket
    else:
        for sku, qty in data["on_hand"].items():
            inventory.buckets[(sku, DEFAULT_LOCATION, None)] = Bucket(
                sku, DEFAULT_LOCATION, None, None, qty,
                data["reserved"].get(sku, 0))
    inventory.movements = list(data.get("movements", []))
    book = OrderBook(inventory)
    book._next_id = data.get("next_order_id", 1)
    for raw in data.get("orders", []):
        order = Order(**raw)
        if not order.picks:
            order.picks = [{"sku": s, "location": DEFAULT_LOCATION,
                            "lot": None, "qty": q}
                           for s, q in order.lines.items()]
        book._orders[order.id] = order
    return inventory, book


def save(path: str, inventory: Inventory, book: OrderBook) -> None:
    """Atomic: a crash mid-save must not eat the warehouse's only record."""
    directory = os.path.dirname(os.path.abspath(path))
    fd, tmp = tempfile.mkstemp(dir=directory, suffix=".tmp")
    with os.fdopen(fd, "w") as f:
        json.dump(to_dict(inventory, book), f, indent=2, sort_keys=True)
    os.replace(tmp, path)


def load(path: str) -> tuple[Inventory, OrderBook]:
    with open(path) as f:
        return from_dict(json.load(f))
