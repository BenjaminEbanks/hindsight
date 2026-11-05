import json

import pytest

from stockroom import importer, reports, store
from stockroom.audit import reconcile
from stockroom.inventory import InsufficientStock, Inventory
from stockroom.orders import OrderBook


def stocked():
    inv = Inventory()
    inv.add_item("A", "Widget", unit_cost_cents=10, reorder_point=2)
    inv.receive("A", 5)
    inv.receive("A", 3, location="east")
    return inv, OrderBook(inv)


def test_levels_per_location_and_in_total():
    inv, _ = stocked()
    assert inv.on_hand("A") == 8
    assert inv.on_hand("A", location="east") == 3
    assert inv.on_hand("A", location="main") == 5
    inv.transfer("A", 2, "main", "east")
    assert inv.on_hand("A", location="east") == 5
    assert inv.on_hand("A", location="main") == 3
    assert inv.on_hand("A") == 8
    with pytest.raises(InsufficientStock):
        inv.transfer("A", 4, "main", "east")


def test_orders_fill_locations_alphabetically_and_cancel_restores():
    inv, book = stocked()
    order = book.place("acme", {"A": 4})
    assert inv.available("A", location="east") == 0
    assert inv.available("A", location="main") == 4
    assert inv.available("A") == 4
    book.cancel(order.id)
    assert inv.available("A", location="east") == 3
    assert inv.available("A", location="main") == 5
    assert reconcile(inv, book) == []


def test_order_from_one_location_and_ship_takes_from_there():
    inv, book = stocked()
    with pytest.raises(InsufficientStock):
        book.place("acme", {"A": 4}, location="east")
    order = book.place("acme", {"A": 2}, location="main")
    assert inv.available("A", location="main") == 3
    book.ship(order.id)
    assert inv.on_hand("A", location="main") == 3
    assert inv.on_hand("A", location="east") == 3
    assert reconcile(inv, book) == []


def test_reserved_stock_cannot_be_transferred_away():
    inv, book = stocked()
    book.place("acme", {"A": 2}, location="east")
    with pytest.raises(InsufficientStock):
        inv.transfer("A", 2, "east", "main")


def test_report_has_one_row_per_sku_and_location():
    inv, book = stocked()
    book.place("acme", {"A": 1}, location="main")
    rows = reports.stock_report(inv)
    assert [(r["sku"], r["location"], r["on_hand"], r["reserved"],
             r["available"]) for r in rows] == [
        ("A", "east", 3, 0, 3), ("A", "main", 5, 1, 4)]
    assert reports.low_stock(inv) == []


def test_importer_location_column():
    inv, _ = stocked()
    importer.import_receipts(inv, "sku,qty,location\nA,4,west\nA,1,\n")
    assert inv.on_hand("A", location="west") == 4
    assert inv.on_hand("A", location="main") == 6


def test_old_save_files_load_into_main_and_their_orders_still_work(tmp_path):
    old = {"items": [{"sku": "A", "name": "Widget", "unit_cost_cents": 10,
                      "reorder_point": 0}],
           "movements": [{"kind": "receive", "sku": "A", "qty": 5, "ref": ""}],
           "next_order_id": 2, "on_hand": {"A": 5},
           "orders": [{"customer": "acme", "id": 1, "lines": {"A": 2},
                       "status": "open"}],
           "reserved": {"A": 2}}
    path = tmp_path / "old.json"
    path.write_text(json.dumps(old))
    inv, book = store.load(str(path))
    assert inv.on_hand("A", location="main") == 5
    assert inv.available("A", location="main") == 3
    book.cancel(1)
    assert inv.available("A", location="main") == 5


def test_round_trip_keeps_where_an_order_took_its_stock(tmp_path):
    inv, book = stocked()
    order = book.place("acme", {"A": 4})
    path = str(tmp_path / "s.json")
    store.save(path, inv, book)
    inv2, book2 = store.load(path)
    assert inv2.on_hand("A", location="east") == 3
    book2.cancel(order.id)
    assert inv2.available("A", location="east") == 3
    assert inv2.available("A", location="main") == 5
