from stockroom.audit import reconcile
from stockroom.inventory import Inventory
from stockroom.orders import OrderBook


def test_clean():
    inv = Inventory()
    inv.add_item("A", "Widget")
    inv.receive("A", 5)
    book = OrderBook(inv)
    order = book.place("acme", {"A": 2})
    assert reconcile(inv, book) == []
    book.ship(order.id)
    assert reconcile(inv, book) == []
