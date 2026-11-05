import pytest

from stockroom.importer import ImportProblem, import_receipts
from stockroom.inventory import Inventory


def inv():
    i = Inventory()
    i.add_item("A", "Widget")
    return i


def test_import():
    i = inv()
    assert import_receipts(i, "sku,qty,ref\nA,3,po-1\nA,2,\n") == 5
    assert i.on_hand("A") == 5


def test_bad_row_applies_nothing():
    i = inv()
    with pytest.raises(ImportProblem):
        import_receipts(i, "sku,qty\nA,3\nA,zero\n")
    assert i.on_hand("A") == 0


def test_missing_column():
    with pytest.raises(ImportProblem):
        import_receipts(inv(), "sku\nA\n")
