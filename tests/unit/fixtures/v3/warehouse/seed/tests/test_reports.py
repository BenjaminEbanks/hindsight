from stockroom import reports
from stockroom.inventory import Inventory
from stockroom.orders import OrderBook


def setup_stock():
    inv = Inventory()
    inv.add_item("A", "Widget", unit_cost_cents=100, reorder_point=5)
    inv.add_item("B", "Gadget", unit_cost_cents=10, reorder_point=0)
    inv.receive("A", 6)
    inv.receive("B", 3)
    book = OrderBook(inv)
    book.place("acme", {"A": 2})
    return inv, book


def test_low_stock_uses_available():
    inv, _ = setup_stock()
    assert reports.low_stock(inv) == ["A"]


def test_value():
    inv, _ = setup_stock()
    assert reports.stock_value_cents(inv) == 630


def test_reorder_suggestions():
    inv, _ = setup_stock()
    assert reports.reorder_suggestions(inv) == {"A": 6}


def test_backlog_and_customers():
    inv, book = setup_stock()
    assert reports.order_backlog(book) == {"A": 2}
    assert reports.customer_summary(book)["acme"]["open"] == 1
