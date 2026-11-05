import pytest

from stockroom import reports
from stockroom.inventory import Inventory, UnknownItem
from stockroom.orders import OrderBook


def activity():
    inv = Inventory()
    inv.add_item("A", "Widget")
    inv.add_item("B", "Gadget")
    inv.receive("A", 20, ref="po-1")
    inv.receive("B", 5)
    book = OrderBook(inv)
    first = book.place("acme", {"A": 3, "B": 1})
    second = book.place("acme", {"A": 2})
    third = book.place("beta", {"A": 4})
    book.place("gamma", {"A": 1})
    book.ship(first.id)
    book.ship(second.id)
    book.cancel(third.id)
    return inv, book


def test_units_shipped_by_customer():
    _, book = activity()
    assert reports.units_shipped_by_customer(book) == {"acme": 6}


def test_movement_totals():
    inv, _ = activity()
    assert reports.movement_totals(inv, "B") == {"receive": 5, "reserve": 1, "ship": 1}
    assert reports.movement_totals(inv, "A") == {
        "receive": 20, "reserve": 10, "ship": 5, "release": 4}
    with pytest.raises(UnknownItem):
        reports.movement_totals(inv, "nope")
