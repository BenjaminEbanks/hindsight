"""Supplier receipts arrive as CSV with a header: sku,qty[,ref][,location]."""
from __future__ import annotations

import csv
import io

from .inventory import DEFAULT_LOCATION, Inventory


class ImportProblem(ValueError):
    def __init__(self, line: int, reason: str) -> None:
        super().__init__(f"line {line}: {reason}")
        self.line = line
        self.reason = reason


def parse_receipts(text: str) -> list[dict]:
    """Validate everything first: a bad row must not half-apply a file."""
    reader = csv.DictReader(io.StringIO(text))
    missing = {"sku", "qty"} - set(reader.fieldnames or [])
    if missing:
        raise ImportProblem(1, f"missing columns: {sorted(missing)}")
    rows = []
    for number, raw in enumerate(reader, start=2):
        sku = (raw.get("sku") or "").strip()
        if not sku:
            raise ImportProblem(number, "empty sku")
        try:
            qty = int(raw["qty"])
        except (TypeError, ValueError):
            raise ImportProblem(number, f"bad qty {raw['qty']!r}") from None
        if qty <= 0:
            raise ImportProblem(number, "qty must be positive")
        rows.append({"sku": sku, "qty": qty,
                     "ref": (raw.get("ref") or "").strip(),
                     "location": (raw.get("location") or "").strip()
                     or DEFAULT_LOCATION})
    return rows


def import_receipts(inventory: Inventory, text: str) -> int:
    """Apply a receipts file; returns the units received."""
    rows = parse_receipts(text)
    for row in rows:
        inventory.item(row["sku"])
    for row in rows:
        inventory.receive(row["sku"], row["qty"], location=row["location"],
                          ref=row["ref"])
    return sum(r["qty"] for r in rows)
