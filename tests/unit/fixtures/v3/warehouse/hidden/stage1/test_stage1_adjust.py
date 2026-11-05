import pytest

from stockroom.audit import reconcile
from stockroom.inventory import InsufficientStock, Inventory
from stockroom.orders import OrderBook


def test_adjust_changes_stock_and_stays_reconciled():
    inv = Inventory()
    inv.add_item("A", "Widget")
    inv.receive("A", 10)
    book = OrderBook(inv)
    book.place("acme", {"A": 4})
    inv.adjust("A", -2, "cycle count")
    assert inv.on_hand("A") == 8
    assert inv.available("A") == 4
    assert inv.movements[-1]["kind"] == "adjust"
    assert inv.movements[-1]["ref"] == "cycle count"
    assert reconcile(inv, book) == []
    inv.adjust("A", 1, "found one")
    assert inv.on_hand("A") == 9
    assert reconcile(inv, book) == []
    with pytest.raises(InsufficientStock):
        inv.adjust("A", -6, "too far")
    assert inv.on_hand("A") == 9
