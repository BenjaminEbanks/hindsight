"""Stock levels. One number per SKU: we have one stockroom."""
from __future__ import annotations

from .models import Item


class InsufficientStock(Exception):
    """A request needed more units than are available."""


class UnknownItem(KeyError):
    """The SKU has not been added to the catalog."""


class Inventory:
    def __init__(self) -> None:
        self.items: dict[str, Item] = {}
        self._on_hand: dict[str, int] = {}
        self._reserved: dict[str, int] = {}
        self.movements: list[dict] = []

    # ------------------------------------------------------------ catalog
    def add_item(self, sku: str, name: str, unit_cost_cents: int = 0,
                 reorder_point: int = 0) -> Item:
        if sku in self.items:
            raise ValueError(f"duplicate sku {sku}")
        item = Item(sku, name, unit_cost_cents, reorder_point)
        self.items[sku] = item
        self._on_hand[sku] = 0
        self._reserved[sku] = 0
        return item

    def item(self, sku: str) -> Item:
        try:
            return self.items[sku]
        except KeyError:
            raise UnknownItem(sku) from None

    def skus(self) -> list[str]:
        return sorted(self.items)

    # ------------------------------------------------------------- levels
    def on_hand(self, sku: str) -> int:
        self.item(sku)
        return self._on_hand[sku]

    def reserved(self, sku: str) -> int:
        self.item(sku)
        return self._reserved[sku]

    def available(self, sku: str) -> int:
        return self.on_hand(sku) - self.reserved(sku)

    # ---------------------------------------------------------- mutations
    def receive(self, sku: str, qty: int, ref: str = "") -> None:
        self.item(sku)
        if qty <= 0:
            raise ValueError("qty must be positive")
        self._on_hand[sku] += qty
        self._log("receive", sku, qty, ref)

    def adjust(self, sku: str, delta: int, reason: str) -> None:
        """A correction from a physical count; `reason` is the log's ref."""
        self.item(sku)
        if delta < 0 and -delta > self.available(sku):
            raise InsufficientStock(f"{sku}: cannot adjust by {delta}")
        self._on_hand[sku] += delta
        self._log("adjust", sku, delta, reason)

    def reserve(self, sku: str, qty: int, ref: str = "") -> None:
        if qty <= 0:
            raise ValueError("qty must be positive")
        if qty > self.available(sku):
            raise InsufficientStock(
                f"{sku}: wanted {qty}, have {self.available(sku)}")
        self._reserved[sku] += qty
        self._log("reserve", sku, qty, ref)

    def release(self, sku: str, qty: int, ref: str = "") -> None:
        if qty > self.reserved(sku):
            raise ValueError(f"{sku}: only {self.reserved(sku)} reserved")
        self._reserved[sku] -= qty
        self._log("release", sku, qty, ref)

    def ship(self, sku: str, qty: int, ref: str = "") -> None:
        """Reserved units physically leave the building."""
        if qty > self.reserved(sku):
            raise ValueError(f"{sku}: only {self.reserved(sku)} reserved")
        self._reserved[sku] -= qty
        self._on_hand[sku] -= qty
        self._log("ship", sku, qty, ref)

    def _log(self, kind: str, sku: str, qty: int, ref: str) -> None:
        self.movements.append({"kind": kind, "sku": sku, "qty": qty, "ref": ref})
