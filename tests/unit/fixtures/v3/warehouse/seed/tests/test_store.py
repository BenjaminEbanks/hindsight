import json
import os

from stockroom import store
from stockroom.inventory import Inventory
from stockroom.orders import OrderBook

SAMPLE = os.path.join(os.path.dirname(__file__), "..", "data", "sample_stock.json")


def test_round_trip(tmp_path):
    inv = Inventory()
    inv.add_item("A", "Widget", unit_cost_cents=5)
    inv.receive("A", 3)
    book = OrderBook(inv)
    order = book.place("acme", {"A": 2})
    path = str(tmp_path / "s.json")
    store.save(path, inv, book)
    inv2, book2 = store.load(path)
    assert inv2.on_hand("A") == 3
    assert inv2.reserved("A") == 2
    assert book2.get(order.id).customer == "acme"
    assert book2.place("beta", {"A": 1}).id == order.id + 1


def test_sample_file_loads():
    inv, book = store.load(SAMPLE)
    assert inv.available("BOLT") == 170
    assert book.open_orders()[0].lines == {"BOLT": 30}


def test_save_is_json(tmp_path):
    inv = Inventory()
    path = str(tmp_path / "s.json")
    store.save(path, inv, OrderBook(inv))
    assert json.load(open(path))["items"] == []
