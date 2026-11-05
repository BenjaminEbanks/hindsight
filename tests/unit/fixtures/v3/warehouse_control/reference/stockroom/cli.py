"""The warehouse team's command line."""
from __future__ import annotations

import argparse
import os
import sys

from . import audit, importer, reports, store
from .inventory import Inventory
from .orders import OrderBook


def _open(path: str) -> tuple[Inventory, OrderBook]:
    if os.path.exists(path):
        return store.load(path)
    inventory = Inventory()
    return inventory, OrderBook(inventory)


def _parse_lines(pairs: list[str]) -> dict[str, int]:
    lines = {}
    for pair in pairs:
        sku, _, qty = pair.partition("=")
        lines[sku] = int(qty)
    return lines


def main(argv: list[str] | None = None, out=None) -> int:
    out = out or sys.stdout
    ap = argparse.ArgumentParser(prog="stockroom")
    ap.add_argument("--file", required=True)
    sub = ap.add_subparsers(dest="cmd", required=True)
    add = sub.add_parser("add-item")
    add.add_argument("sku")
    add.add_argument("name")
    add.add_argument("--reorder", type=int, default=0)
    rec = sub.add_parser("receive")
    rec.add_argument("sku")
    rec.add_argument("qty", type=int)
    imp = sub.add_parser("import")
    imp.add_argument("csv_path")
    plc = sub.add_parser("place")
    plc.add_argument("customer")
    plc.add_argument("lines", nargs="+", help="sku=qty")
    can = sub.add_parser("cancel")
    can.add_argument("order_id", type=int)
    shp = sub.add_parser("ship")
    shp.add_argument("order_id", type=int)
    sub.add_parser("status")
    sub.add_parser("stock-csv")
    sub.add_parser("audit")
    args = ap.parse_args(argv)

    inventory, book = _open(args.file)
    code = 0
    if args.cmd == "add-item":
        inventory.add_item(args.sku, args.name, reorder_point=args.reorder)
    elif args.cmd == "receive":
        inventory.receive(args.sku, args.qty)
    elif args.cmd == "import":
        with open(args.csv_path) as f:
            print(importer.import_receipts(inventory, f.read()), file=out)
    elif args.cmd == "place":
        print(book.place(args.customer, _parse_lines(args.lines)).id, file=out)
    elif args.cmd == "cancel":
        book.cancel(args.order_id)
    elif args.cmd == "ship":
        book.ship(args.order_id)
    elif args.cmd == "status":
        for row in reports.stock_report(inventory):
            print(f"{row['sku']} on_hand={row['on_hand']} "
                  f"available={row['available']}", file=out)
    elif args.cmd == "stock-csv":
        out.write(reports.stock_csv(inventory))
    elif args.cmd == "audit":
        for problem in audit.reconcile(inventory, book):
            print(problem, file=out)
            code = 1
    store.save(args.file, inventory, book)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
