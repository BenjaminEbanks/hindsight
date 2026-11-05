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
class Order:
    id: int
    customer: str
    lines: dict[str, int] = field(default_factory=dict)
    status: str = "open"  # open | shipped | cancelled

    def total_units(self) -> int:
        return sum(self.lines.values())
