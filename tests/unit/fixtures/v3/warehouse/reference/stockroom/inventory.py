"""Stock levels, as buckets of (sku, location, lot)."""
from __future__ import annotations

from .models import Bucket, Item

DEFAULT_LOCATION = "main"


class InsufficientStock(Exception):
    """A request needed more units than are available."""


class UnknownItem(KeyError):
    """The SKU has not been added to the catalog."""


class Inventory:
    def __init__(self) -> None:
        self.items: dict[str, Item] = {}
        self.buckets: dict[tuple, Bucket] = {}
        self.movements: list[dict] = []

    # ------------------------------------------------------------ catalog
    def add_item(self, sku: str, name: str, unit_cost_cents: int = 0,
                 reorder_point: int = 0) -> Item:
        if sku in self.items:
            raise ValueError(f"duplicate sku {sku}")
        item = Item(sku, name, unit_cost_cents, reorder_point)
        self.items[sku] = item
        return item

    def item(self, sku: str) -> Item:
        try:
            return self.items[sku]
        except KeyError:
            raise UnknownItem(sku) from None

    def skus(self) -> list[str]:
        return sorted(self.items)

    # ------------------------------------------------------------- levels
    def buckets_of(self, sku: str, location: str | None = None) -> list[Bucket]:
        self.item(sku)
        return sorted(
            (b for b in self.buckets.values()
             if b.sku == sku and (location is None or b.location == location)),
            key=Bucket.order_key)

    def locations(self, sku: str) -> list[str]:
        return sorted({b.location for b in self.buckets_of(sku)})

    def on_hand(self, sku: str, location: str | None = None) -> int:
        return sum(b.on_hand for b in self.buckets_of(sku, location))

    def reserved(self, sku: str, location: str | None = None) -> int:
        return sum(b.reserved for b in self.buckets_of(sku, location))

    def available(self, sku: str, location: str | None = None,
                  as_of: str | None = None) -> int:
        return sum(b.free for b in self._eligible(sku, location, as_of))

    def _eligible(self, sku, location, as_of) -> list[Bucket]:
        return [b for b in self.buckets_of(sku, location)
                if as_of is None or b.expires is None or b.expires >= as_of]

    # ---------------------------------------------------------- mutations
    def _bucket(self, sku, location, lot=None, expires=None) -> Bucket:
        key = (sku, location, lot)
        if key not in self.buckets:
            self.buckets[key] = Bucket(sku, location, lot, expires)
        return self.buckets[key]

    def receive(self, sku: str, qty: int, location: str = DEFAULT_LOCATION,
                lot: str | None = None, expires: str | None = None,
                ref: str = "") -> None:
        self.item(sku)
        if qty <= 0:
            raise ValueError("qty must be positive")
        self._bucket(sku, location, lot, expires).on_hand += qty
        self._log("receive", sku, qty, ref, location=location, lot=lot)

    def adjust(self, sku: str, delta: int, reason: str,
               location: str = DEFAULT_LOCATION) -> None:
        self.item(sku)
        if delta >= 0:
            self._bucket(sku, location).on_hand += delta
        else:
            if -delta > self.available(sku, location):
                raise InsufficientStock(f"{sku}: cannot adjust by {delta}")
            left = -delta
            for bucket in self.buckets_of(sku, location):
                take = min(left, bucket.free)
                bucket.on_hand -= take
                left -= take
        self._log("adjust", sku, delta, reason, location=location)

    def transfer(self, sku: str, qty: int, src: str, dst: str) -> None:
        if qty <= 0:
            raise ValueError("qty must be positive")
        if qty > self.available(sku, src):
            raise InsufficientStock(
                f"{sku}: wanted {qty} from {src}, have {self.available(sku, src)}")
        left = qty
        for bucket in self.buckets_of(sku, src):
            take = min(left, bucket.free)
            if take:
                bucket.on_hand -= take
                self._bucket(sku, dst, bucket.lot, bucket.expires).on_hand += take
                left -= take
        self._log("transfer", sku, qty, f"{src}->{dst}")

    def allocate(self, sku: str, qty: int, location: str | None = None,
                 as_of: str | None = None, ref: str = "") -> list[dict]:
        """Reserve `qty`, first expired first out; returns the picks."""
        if qty <= 0:
            raise ValueError("qty must be positive")
        if qty > self.available(sku, location, as_of):
            raise InsufficientStock(
                f"{sku}: wanted {qty}, have {self.available(sku, location, as_of)}")
        picks, left = [], qty
        for bucket in self._eligible(sku, location, as_of):
            take = min(left, bucket.free)
            if take:
                bucket.reserved += take
                picks.append({"sku": sku, "location": bucket.location,
                              "lot": bucket.lot, "qty": take})
                left -= take
        self._log("reserve", sku, qty, ref)
        return picks

    def reserve(self, sku: str, qty: int, ref: str = "") -> list[dict]:
        return self.allocate(sku, qty, ref=ref)

    def release_picks(self, picks: list[dict], ref: str = "") -> None:
        for pick in picks:
            self._bucket(pick["sku"], pick["location"], pick["lot"]).reserved -= pick["qty"]
            self._log("release", pick["sku"], pick["qty"], ref)

    def ship_picks(self, picks: list[dict], ref: str = "") -> None:
        for pick in picks:
            bucket = self._bucket(pick["sku"], pick["location"], pick["lot"])
            bucket.reserved -= pick["qty"]
            bucket.on_hand -= pick["qty"]
            self._log("ship", pick["sku"], pick["qty"], ref)

    def _reserved_picks(self, sku: str, qty: int) -> list[dict]:
        if qty > self.reserved(sku):
            raise ValueError(f"{sku}: only {self.reserved(sku)} reserved")
        picks, left = [], qty
        for bucket in self.buckets_of(sku):
            take = min(left, bucket.reserved)
            if take:
                picks.append({"sku": sku, "location": bucket.location,
                              "lot": bucket.lot, "qty": take})
                left -= take
        return picks

    def release(self, sku: str, qty: int, ref: str = "") -> None:
        self.release_picks(self._reserved_picks(sku, qty), ref)

    def ship(self, sku: str, qty: int, ref: str = "") -> None:
        self.ship_picks(self._reserved_picks(sku, qty), ref)

    def _log(self, kind, sku, qty, ref, **extra) -> None:
        self.movements.append(
            {"kind": kind, "sku": sku, "qty": qty, "ref": ref, **extra})
