import pytest

from stockroom.inventory import InsufficientStock, Inventory
from stockroom.orders import OrderBook


@pytest.fixture
def book():
    inv = Inventory()
    inv.add_item("A", "Widget")
    inv.add_item("B", "Gadget")
    inv.receive("A", 10)
    inv.receive("B", 1)
    return OrderBook(inv)


def test_place_reserves(book):
    order = book.place("acme", {"A": 4})
    assert order.id == 1
    assert book.inventory.reserved("A") == 4


def test_place_is_all_or_nothing(book):
    with pytest.raises(InsufficientStock):
        book.place("acme", {"A": 4, "B": 2})
    assert book.inventory.reserved("A") == 0


def test_cancel_releases(book):
    order = book.place("acme", {"A": 4})
    book.cancel(order.id)
    assert book.inventory.available("A") == 10
    assert order.status == "cancelled"


def test_ship_consumes(book):
    order = book.place("acme", {"A": 4})
    book.ship(order.id)
    assert book.inventory.on_hand("A") == 6
    assert order.status == "shipped"


def test_cannot_cancel_twice(book):
    order = book.place("acme", {"A": 1})
    book.cancel(order.id)
    with pytest.raises(ValueError):
        book.cancel(order.id)


def test_empty_order_refused(book):
    with pytest.raises(ValueError):
        book.place("acme", {})
