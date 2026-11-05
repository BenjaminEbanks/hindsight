"""Plain data records shared across the package."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Item:
    sku: str
    name: str
    unit_cost_cents: int = 0
    reorder_point: int = 0


@dataclass
class Bucket:
    """Units of one SKU in one location from one lot."""
    sku: str
    location: str
    lot: str | None = None
    expires: str | None = None
    on_hand: int = 0
    reserved: int = 0

    @property
    def free(self) -> int:
        return self.on_hand - self.reserved

    @property
    def key(self) -> tuple:
        return (self.sku, self.location, self.lot)

    def order_key(self) -> tuple:
        return (self.expires is None, self.expires or "", self.location,
                self.lot is not None, self.lot or "")


@dataclass
class Order:
    id: int
    customer: str
    lines: dict[str, int] = field(default_factory=dict)
    status: str = "open"  # open | shipped | cancelled
    picks: list[dict] = field(default_factory=list)

    def total_units(self) -> int:
        return sum(self.lines.values())
