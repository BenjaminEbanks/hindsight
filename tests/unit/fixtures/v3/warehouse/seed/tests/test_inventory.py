import pytest

from stockroom.inventory import InsufficientStock, Inventory, UnknownItem


@pytest.fixture
def inv():
    i = Inventory()
    i.add_item("A", "Widget", unit_cost_cents=100, reorder_point=2)
    return i


def test_receive_and_levels(inv):
    inv.receive("A", 5)
    assert inv.on_hand("A") == 5
    assert inv.available("A") == 5


def test_reserve_reduces_available_not_on_hand(inv):
    inv.receive("A", 5)
    inv.reserve("A", 3)
    assert inv.on_hand("A") == 5
    assert inv.available("A") == 2


def test_overreserve_refused(inv):
    inv.receive("A", 1)
    with pytest.raises(InsufficientStock):
        inv.reserve("A", 2)


def test_ship_consumes_reserved(inv):
    inv.receive("A", 5)
    inv.reserve("A", 3)
    inv.ship("A", 3)
    assert inv.on_hand("A") == 2
    assert inv.reserved("A") == 0


def test_release(inv):
    inv.receive("A", 5)
    inv.reserve("A", 3)
    inv.release("A", 2)
    assert inv.available("A") == 4


def test_unknown_sku(inv):
    with pytest.raises(UnknownItem):
        inv.on_hand("nope")


def test_duplicate_sku(inv):
    with pytest.raises(ValueError):
        inv.add_item("A", "again")


def test_movements_logged(inv):
    inv.receive("A", 5, ref="po-1")
    move = inv.movements[0]
    assert (move["kind"], move["sku"], move["qty"], move["ref"]) == (
        "receive", "A", 5, "po-1")
