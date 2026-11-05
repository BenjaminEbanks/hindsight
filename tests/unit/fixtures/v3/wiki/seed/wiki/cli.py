"""The command line the team uses."""
from __future__ import annotations

import argparse
import sys

from . import export, graph
from .notebook import Notebook


def main(argv: list[str] | None = None, out=None) -> int:
    out = out or sys.stdout
    ap = argparse.ArgumentParser(prog="wiki")
    ap.add_argument("--root", required=True)
    sub = ap.add_subparsers(dest="cmd", required=True)
    new = sub.add_parser("new")
    new.add_argument("title")
    new.add_argument("body", nargs="?", default="")
    new.add_argument("--tag", action="append", default=[])
    show = sub.add_parser("show")
    show.add_argument("title")
    find = sub.add_parser("search")
    find.add_argument("query")
    sub.add_parser("check")
    site = sub.add_parser("export")
    site.add_argument("out_dir")
    args = ap.parse_args(argv)

    nb = Notebook(args.root)
    if args.cmd == "new":
        nb.create(args.title, args.body, args.tag)
    elif args.cmd == "show":
        print(nb.get(args.title).body, file=out)
    elif args.cmd == "search":
        for title in nb.search(args.query):
            print(title, file=out)
    elif args.cmd == "check":
        problems = graph.broken_links(nb)
        for title, bad in problems.items():
            print(f"{title}: {', '.join(bad)}", file=out)
        return 1 if problems else 0
    elif args.cmd == "export":
        export.export_html(nb, args.out_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
