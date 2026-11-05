import json

from stockroom import reports, store
from stockroom.audit import reconcile
from stockroom.inventory import Inventory
from stockroom.orders import OrderBook


def stocked():
    inv = Inventory()
    inv.add_item("M", "Milk", unit_cost_cents=50)
    inv.receive("M", 4, location="main", lot="L1", expires="2030-03-10")
    inv.receive("M", 4, location="east", lot="L2", expires="2030-03-05")
    inv.receive("M", 4, location="main")  # no expiry
    return inv, OrderBook(inv)


def picks(book, order):
    return [(p["sku"], p["location"], p["lot"], p["qty"])
            for p in book.picks(order.id)]


def test_earliest_expiry_wins_across_locations():
    inv, book = stocked()
    order = book.place("acme", {"M": 6})
    assert picks(book, order) == [("M", "east", "L2", 4), ("M", "main", "L1", 2)]


def test_stock_without_expiry_goes_last():
    inv, book = stocked()
    order = book.place("acme", {"M": 10})
    assert picks(book, order)[-1] == ("M", "main", None, 2)


def test_expired_stock_is_not_available_or_allocated():
    inv, book = stocked()
    assert inv.available("M", as_of="2030-03-06") == 8
    assert inv.available("M", as_of="2030-03-05") == 12
    order = book.place("acme", {"M": 2}, as_of="2030-03-06")
    assert picks(book, order) == [("M", "main", "L1", 2)]
    assert inv.on_hand("M") == 12


def test_cancel_and_ship_use_the_exact_picks():
    inv, book = stocked()
    first = book.place("acme", {"M": 2})
    second = book.place("beta", {"M": 3})
    assert picks(book, second) == [("M", "east", "L2", 2), ("M", "main", "L1", 1)]
    book.cancel(first.id)
    third = book.place("gamma", {"M": 2})
    assert picks(book, third) == [("M", "east", "L2", 2)]
    book.ship(second.id)
    assert inv.on_hand("M", location="main") == 7
    assert inv.on_hand("M", location="east") == 2
    assert reconcile(inv, book) == []


def test_picks_survive_save_and_load(tmp_path):
    inv, book = stocked()
    order = book.place("acme", {"M": 6})
    before = picks(book, order)
    path = str(tmp_path / "s.json")
    store.save(path, inv, book)
    inv2, book2 = store.load(path)
    assert picks(book2, order) == before
    assert inv2.available("M", as_of="2030-03-06") == 6
    book2.cancel(order.id)
    assert inv2.available("M", location="east") == 4
    follow_up = book2.place("beta", {"M": 1})
    assert picks(book2, follow_up) == [("M", "east", "L2", 1)]


def test_transfer_moves_earliest_expiry_and_keeps_the_lot():
    inv, book = stocked()
    inv.transfer("M", 5, "main", "west")
    assert inv.on_hand("M", location="west") == 5
    rows = reports.expiring(inv, "2030-03-31")
    assert [(r["sku"], r["location"], r["lot"], r["expires"], r["on_hand"])
            for r in rows] == [
        ("M", "east", "L2", "2030-03-05", 4),
        ("M", "west", "L1", "2030-03-10", 4)]
    assert reports.expiring(inv, "2030-03-06")[0]["lot"] == "L2"
    assert len(reports.expiring(inv, "2030-03-06")) == 1


def test_old_save_file_order_has_main_no_lot_picks(tmp_path):
    old = {"items": [{"sku": "A", "name": "Widget", "unit_cost_cents": 10,
                      "reorder_point": 0}],
           "movements": [{"kind": "receive", "sku": "A", "qty": 5, "ref": ""}],
           "next_order_id": 2, "on_hand": {"A": 5},
           "orders": [{"customer": "acme", "id": 1, "lines": {"A": 2},
                       "status": "open"}],
           "reserved": {"A": 2}}
    path = tmp_path / "old.json"
    path.write_text(json.dumps(old))
    _, book = store.load(str(path))
    assert [(p["location"], p["lot"], p["qty"]) for p in book.picks(1)] == [
        ("main", None, 2)]
