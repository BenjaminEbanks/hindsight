import io

from stockroom import reports
from stockroom.cli import main
from stockroom.inventory import Inventory
from stockroom.orders import OrderBook

EXPECTED = (
    "sku,name,on_hand,reserved,available\n"
    "A,Widget,5,2,3\n"
    "B,Gadget,0,0,0\n")


def stocked():
    inv = Inventory()
    inv.add_item("A", "Widget")
    inv.add_item("B", "Gadget")
    inv.receive("A", 5)
    book = OrderBook(inv)
    book.place("acme", {"A": 2})
    return inv, book


def test_orders_for_customer():
    _, book = stocked()
    second = book.place("beta", {"A": 1})
    third = book.place("acme", {"A": 1})
    assert [o.id for o in book.orders_for("acme")] == [1, third.id]
    assert [o.id for o in book.orders_for("beta")] == [second.id]
    assert book.orders_for("nobody") == []


def test_stock_csv():
    inv, _ = stocked()
    assert reports.stock_csv(inv) == EXPECTED


def test_stock_csv_command(tmp_path):
    path = str(tmp_path / "s.json")
    for args in (["add-item", "A", "Widget"], ["add-item", "B", "Gadget"],
                 ["receive", "A", "5"], ["place", "acme", "A=2"]):
        assert main(["--file", path, *args], io.StringIO()) == 0
    out = io.StringIO()
    assert main(["--file", path, "stock-csv"], out) == 0
    assert out.getvalue() == EXPECTED
