"""Stock levels. The local patch: key the on-hand dict by (sku, location)."""
from __future__ import annotations

from .models import Item


class InsufficientStock(Exception):
    """A request needed more units than are available."""


class UnknownItem(KeyError):
    """The SKU has not been added to the catalog."""


class Inventory:
    def __init__(self) -> None:
        self.items: dict[str, Item] = {}
        self._on_hand: dict[tuple, int] = {}
        self._reserved: dict[str, int] = {}
        self.movements: list[dict] = []

    def add_item(self, sku: str, name: str, unit_cost_cents: int = 0,
                 reorder_point: int = 0) -> Item:
        if sku in self.items:
            raise ValueError(f"duplicate sku {sku}")
        item = Item(sku, name, unit_cost_cents, reorder_point)
        self.items[sku] = item
        self._reserved[sku] = 0
        return item

    def item(self, sku: str) -> Item:
        try:
            return self.items[sku]
        except KeyError:
            raise UnknownItem(sku) from None

    def skus(self) -> list[str]:
        return sorted(self.items)

    def on_hand(self, sku: str, location: str | None = None) -> int:
        self.item(sku)
        return sum(q for (s, loc), q in self._on_hand.items()
                   if s == sku and (location is None or loc == location))

    def reserved(self, sku: str) -> int:
        self.item(sku)
        return self._reserved[sku]

    def available(self, sku: str, location: str | None = None) -> int:
        if location is None:
            return self.on_hand(sku) - self.reserved(sku)
        return self.on_hand(sku, location)

    def receive(self, sku: str, qty: int, location: str = "main",
                ref: str = "") -> None:
        self.item(sku)
        if qty <= 0:
            raise ValueError("qty must be positive")
        key = (sku, location)
        self._on_hand[key] = self._on_hand.get(key, 0) + qty
        self._log("receive", sku, qty, ref)

    def adjust(self, sku: str, delta: int, reason: str,
               location: str = "main") -> None:
        if self.on_hand(sku) + delta < self.reserved(sku):
            raise InsufficientStock(f"{sku}: cannot adjust by {delta}")
        key = (sku, location)
        self._on_hand[key] = self._on_hand.get(key, 0) + delta
        self._log("adjust", sku, delta, reason)

    def transfer(self, sku: str, qty: int, src: str, dst: str) -> None:
        if qty > self.on_hand(sku, src):
            raise InsufficientStock(f"{sku}: not enough in {src}")
        self._on_hand[(sku, src)] -= qty
        self._on_hand[(sku, dst)] = self._on_hand.get((sku, dst), 0) + qty

    def reserve(self, sku: str, qty: int, ref: str = "") -> None:
        if qty > self.available(sku):
            raise InsufficientStock(f"{sku}: wanted {qty}")
        self._reserved[sku] += qty
        self._log("reserve", sku, qty, ref)

    def release(self, sku: str, qty: int, ref: str = "") -> None:
        self._reserved[sku] -= qty
        self._log("release", sku, qty, ref)

    def ship(self, sku: str, qty: int, ref: str = "") -> None:
        self._reserved[sku] -= qty
        left = qty
        for (s, loc) in sorted(self._on_hand):
            if s == sku:
                take = min(left, self._on_hand[(s, loc)])
                self._on_hand[(s, loc)] -= take
                left -= take
        self._log("ship", sku, qty, ref)

    def _log(self, kind: str, sku: str, qty: int, ref: str) -> None:
        self.movements.append({"kind": kind, "sku": sku, "qty": qty, "ref": ref})
