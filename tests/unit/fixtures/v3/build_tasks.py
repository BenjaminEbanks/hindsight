"""Build eval/tasks/v3/*.json from the fixtures next to this file.

The seed repos and hidden tests live here as real files so they can be read and
tested; the JSON is what the runner loads. test_eval_tasks_v3.py fails if the two
drift. Rebuild from the repo root: python tests/unit/fixtures/v3/build_tasks.py
"""
from __future__ import annotations

import json
from pathlib import Path

HERE = Path(__file__).parent
OUT = HERE.parents[3] / "eval" / "tasks" / "v3"

ADJUST_MESSAGE = (
    "This is our stockroom tool, have a look around. First small thing: we need "
    "stock corrections after physical counts. Add `Inventory.adjust(sku, delta, "
    "reason)` in stockroom/inventory.py. delta can be positive or negative and "
    "changes on hand. Log it in `movements` with kind \"adjust\", qty = delta and "
    "ref = reason, and make the nightly audit (`audit.reconcile`) still come back "
    "clean afterwards. If a negative adjust would take more than is available "
    "(on hand minus reserved) raise InsufficientStock and change nothing."
)

TASKS = {
    "warehouse_locations_lots": {
        "title": "Stockroom grows from one number per SKU to locations and lots",
        "no_change": False,
        "seed": "warehouse/seed",
        "stages": [
            {"message": ADJUST_MESSAGE,
             "requirements": [
                 ("adjust", "Inventory.adjust(sku, delta, reason) changes on hand and logs an adjust movement"),
                 ("adjust-audit", "reconcile stays clean after adjustments"),
                 ("adjust-guard", "A negative adjust beyond available raises InsufficientStock and changes nothing"),
             ],
             "hidden": "warehouse/hidden/stage1"},
            {"message": (
                "Thanks. Bigger news: we took a second room, so stock now lives in named "
                "locations. All the existing behaviour must keep working, and the warehouse "
                "has years of save files, so files in today's format (see "
                "data/sample_stock.json: on_hand and reserved per SKU, orders without "
                "location info) must still load. Everything in an old file counts as being "
                "in location \"main\", including what its open orders reserved, so those "
                "orders can still be cancelled or shipped. What I need:\n"
                "- `receive(sku, qty, location=\"main\", ref=\"\")`: keyword location, default \"main\".\n"
                "- `on_hand`, `reserved` and `available` take an optional `location=` keyword; "
                "without it they give the total across locations.\n"
                "- `transfer(sku, qty, src, dst)` moves units between locations. Reserved units "
                "can't move: if qty is more than what's available at src, raise "
                "InsufficientStock.\n"
                "- `OrderBook.place(customer, lines, location=None)`: with a location, the "
                "whole order comes from there. Without one, each line takes from locations in "
                "alphabetical order until it's covered. Not enough stock raises "
                "InsufficientStock as today.\n"
                "- Cancel and ship must act on exactly the locations that order reserved from, "
                "also after a save and load in between, whichever save format you end up with.\n"
                "- `reports.stock_report` gets one row per SKU and location, sorted by sku then "
                "location, each row keeping its keys and adding \"location\". `low_stock` stays "
                "based on the total available per SKU.\n"
                "- The receipts CSV importer accepts an optional `location` column; blank or "
                "missing means \"main\".\n"
                "- `audit.reconcile` must still come back clean through all of this."),
             "requirements": [
                 ("locations", "Stock is tracked per named location with totals across locations"),
                 ("transfer", "transfer moves units and refuses more than is available at the source"),
                 ("order-fill", "Orders fill from one location or alphabetically across locations"),
                 ("exact-release", "Cancel and ship act on the locations the order reserved, across save and load"),
                 ("old-files", "Save files in the old format load into main and their open orders keep working"),
                 ("report-rows", "stock_report has one row per SKU and location"),
                 ("import-location", "The importer accepts an optional location column"),
             ],
             "hidden": "warehouse/hidden/stage2"},
            {"message": (
                "That works, nice. Now the food customer: their stock has lots and expiry "
                "dates, and we have to ship what expires first no matter which room it's in. "
                "Dates are ISO strings like \"2030-03-10\". Everything from before keeps "
                "working, old save files included, and this changes how stock is picked:\n"
                "- `receive(..., lot=None, expires=None)`: new keywords. Units with different "
                "lots are separate even in the same location.\n"
                "- `available(sku, location=None, as_of=None)` and `OrderBook.place(customer, "
                "lines, location=None, as_of=None)`: with as_of, units whose expiry is before "
                "that date don't count and can't be picked (expiring on that date is still "
                "fine). They stay in on_hand. Without as_of nothing is treated as expired; "
                "never read the system clock.\n"
                "- Pick order, replacing the alphabetical rule: earliest expiry first, stock "
                "with no expiry last. Ties go by location alphabetically, then units with a lot "
                "before units without.\n"
                "- New `OrderBook.picks(order_id)` returns the list of picks in the order they "
                "were made, each a dict with keys \"sku\", \"location\", \"lot\" (None if no lot) "
                "and \"qty\". Cancel and ship act on exactly those picks, and they survive a "
                "save and load. An open order from an old save file has one pick per line: "
                "location \"main\", lot None.\n"
                "- `transfer` moves earliest-expiry units first under the same pick order, and "
                "units keep their lot and expiry when they move.\n"
                "- New `reports.expiring(inventory, by)`: a list of dicts with keys \"sku\", "
                "\"location\", \"lot\", \"expires\" and \"on_hand\", one per lot in a location, for "
                "stock expiring on or before `by` with on_hand above zero, sorted by expires "
                "then location."),
             "requirements": [
                 ("lots", "Stock is tracked per lot and expiry within a location"),
                 ("fefo", "Picking is earliest expiry first across locations, no-expiry stock last"),
                 ("as-of", "as_of excludes expired units from available and from picking without using the clock"),
                 ("picks", "OrderBook.picks returns exact picks, used by cancel and ship, surviving save and load"),
                 ("transfer-lots", "Transfers take earliest expiry first and keep lot and expiry"),
                 ("expiring", "reports.expiring lists stock expiring on or before a date"),
             ],
             "hidden": "warehouse/hidden/stage3"},
        ],
    },
    "wiki_stable_identity": {
        "title": "Team wiki where a title stops being a page's identity",
        "no_change": False,
        "seed": "wiki/seed",
        "stages": [
            {"message": (
                "This is our team wiki, take a look. Quick one to start: add "
                "`Notebook.pages_with_tag(tag)` in wiki/notebook.py returning the sorted titles "
                "of pages with that tag, and `graph.tag_counts(nb)` in wiki/graph.py returning "
                "a dict of tag to number of pages. Tags are case-insensitive: "
                "`pages_with_tag(\"PYTHON\")` finds a page tagged \"python\", tag_counts keys are "
                "lowercase. An unknown tag gives an empty list."),
             "requirements": [
                 ("tag-lookup", "pages_with_tag returns sorted titles, ignoring case"),
                 ("tag-counts", "graph.tag_counts maps lowercase tags to page counts"),
             ],
             "hidden": "wiki/hidden/stage1"},
            {"message": (
                "Next, people keep asking to rename pages. Add `Notebook.rename(old, new)`. The "
                "page keeps its body and tags and its file becomes pages/<new slug>.md, no "
                "old file left behind. `get(old)` then raises PageNotFound, and renaming onto a "
                "title that's taken raises PageExists and changes nothing. Here's the catch: "
                "other people's pages are theirs, so we must NOT rewrite their text. A page "
                "saying `[[Old Name]]` keeps exactly that body, and the link keeps working: "
                "`target_of` gives the new title, `graph.backlinks(nb, new)` includes the linker, "
                "`graph.backlinks(nb, old)` is `[]`, it isn't in `graph.broken_links` or counted "
                "as orphaned, and `stats.most_linked()` (still (title, count) pairs) counts it "
                "under the new title. Links written later using a former title reach that page "
                "too, through any number of renames. Search goes by the new title (a linker "
                "whose body says the old name still matches on its own text, of course). "
                "`export.export_html` links to the new file but displays the text as written, "
                "e.g. `<a href=\"new-name.html\">Old Name</a>`, and writes no file for the old "
                "name. All this has to survive `Notebook(root)` being reopened. People also "
                "keep dropping .md files into pages/ by hand in the current format (see "
                "wiki/storage.py), before and after renames, and existing folders have nothing "
                "but those files, so those must keep loading and linking like any other page. "
                "You can keep extra data on disk if you need to, but a page's file stays in "
                "the current format."),
             "requirements": [
                 ("rename", "Notebook.rename moves a page to a new title and file, refusing taken titles"),
                 ("no-rewrite", "Other pages' bodies are not rewritten and their links keep working"),
                 ("consumers", "backlinks, broken_links, orphans, search and export follow renames"),
                 ("old-title-links", "Links written later with a former title reach the renamed page"),
                 ("rename-persists", "Renames survive reopening; hand-dropped legacy files still load and link"),
             ],
             "hidden": "wiki/hidden/stage2"},
            {"message": (
                "Rename is great, and now we've hit the problem with it: someone renamed "
                "\"Alpha\" to \"Beta\" and then made a brand new page called \"Alpha\". Pages that "
                "linked to the original should still mean the original. So titles can come back, "
                "and the rule is that a link belongs to the page it was written for:\n"
                "- When a link is written, it attaches to the page that has that title right "
                "then. If no page does, it attaches to the page that most recently had that "
                "title. If there's none, it's unresolved (`target_of` gives None) and attaches "
                "as soon as a page with that title exists, staying attached if that page is then "
                "renamed.\n"
                "- Once attached it stays attached, whatever happens to titles afterwards. "
                "`target_of`, backlinks, broken_links, orphans, most_linked and export all "
                "follow this.\n"
                "- `update` keeps the attachment of any link text still in the new body. A link "
                "text that gets removed by an update and comes back in a later one counts as "
                "newly written.\n"
                "- Deleting a page breaks links attached to it and its former titles stop "
                "resolving. Those links are unresolved again, same rule as above.\n"
                "- Links in files from existing folders are written, as far as we can tell, the "
                "first time the notebook opens them.\n"
                "All of it survives reopening, and `backup.dump` / `backup.restore` must carry it "
                "too (the dump stays JSON-serializable). Ops has years of old backups with just "
                "the pages list, and those must still restore, with links treated like files "
                "from existing folders."),
             "requirements": [
                 ("title-reuse", "A title can be reused; earlier links stay with the original page"),
                 ("attach-rule", "Links attach at writing to the current or most recent holder of the title, or when it appears"),
                 ("update-keeps", "update keeps attachments of retained link text; removed then re-added links are new"),
                 ("delete-breaks", "Deleting a page breaks its links and retires its former titles"),
                 ("attach-persists", "Attachments survive reopening, including for legacy folders"),
             ],
             "hidden": "wiki/hidden/stage3"},
            {"message": (
                "Last one for now: page history. New `Notebook.history(title)` returns a list of "
                "dicts with exactly the keys \"rev\", \"title\" and \"body\", oldest first, rev counting from 1. "
                "create, update, rename and revert each add a revision, and a rename's revision "
                "has the new title and the same body. History belongs to the page, so it follows "
                "renames: `history(old_title)` raises PageNotFound once nothing has that title, "
                "and a new page reusing a title starts its own history. A page first seen from "
                "an existing file starts at rev 1 with its current body.\n"
                "New `Notebook.revert(title, rev)` adds a new revision with the body from `rev`. "
                "It keeps the current title (and tags), and search reflects the restored body. "
                "An unknown rev raises ValueError. The important part: links go back too. Each "
                "link in the restored body points where it pointed in that revision, not "
                "wherever its text would resolve today. If the page it pointed to has since been "
                "deleted, the link is treated as newly written. History and restored links all "
                "survive reopening and a `backup.dump` / `backup.restore` round trip."),
             "requirements": [
                 ("history", "history lists revisions oldest first, following renames, per page rather than per title"),
                 ("revert", "revert restores a revision's body as a new revision, keeping the title"),
                 ("revert-links", "revert restores where links pointed in that revision"),
                 ("history-persists", "History and restored links survive reopening; legacy pages start at rev 1"),
             ],
             "hidden": "wiki/hidden/stage4"},
        ],
    },
    "warehouse_reports_control": {
        "title": "Stockroom gets additive reports (no-change control)",
        "no_change": True,
        "seed": "warehouse/seed",
        "stages": [
            {"message": ADJUST_MESSAGE,
             "requirements": [
                 ("adjust", "Inventory.adjust(sku, delta, reason) changes on hand and logs an adjust movement"),
                 ("adjust-audit", "reconcile stays clean after adjustments"),
                 ("adjust-guard", "A negative adjust beyond available raises InsufficientStock and changes nothing"),
             ],
             "hidden": "warehouse_control/hidden/stage1"},
            {"message": (
                "Thanks. Two more reports for ops, in stockroom/reports.py. "
                "`units_shipped_by_customer(book)` returns a dict of customer to units on their "
                "shipped orders, leaving out customers with nothing shipped. "
                "`movement_totals(inventory, sku)` returns a dict of movement kind to the sum of "
                "logged qty for that SKU, only kinds that occurred, and raises UnknownItem for "
                "an unknown SKU."),
             "requirements": [
                 ("units-shipped", "units_shipped_by_customer sums units on shipped orders per customer"),
                 ("movement-totals", "movement_totals sums logged qty per kind for a SKU"),
             ],
             "hidden": "warehouse_control/hidden/stage2"},
            {"message": (
                "Last bits. `OrderBook.orders_for(customer)` returns that customer's orders sorted "
                "by id, empty list if none. `reports.stock_csv(inventory)` returns the stock report "
                "as CSV text: header `sku,name,on_hand,reserved,available`, then a row per SKU in "
                "sku order, lines ending in \\n. And a `stock-csv` command in stockroom/cli.py "
                "that prints exactly that text and exits 0."),
             "requirements": [
                 ("orders-for", "orders_for lists a customer's orders by id"),
                 ("stock-csv", "stock_csv renders the stock report as CSV"),
                 ("csv-command", "The stock-csv command prints the CSV"),
             ],
             "hidden": "warehouse_control/hidden/stage3"},
        ],
    },
}


def read_tree(root: Path) -> dict[str, str]:
    return {p.relative_to(root).as_posix(): p.read_text()
            for p in sorted(root.rglob("*"))
            if p.is_file() and "__pycache__" not in p.parts}


def build(task_id: str, spec: dict) -> dict:
    return {
        "id": task_id,
        "title": spec["title"],
        "no_change": spec["no_change"],
        "test_command": "python3 -m pytest -q",
        "hidden_test_command": ("uv run --no-project --with pytest python -m "
                                "pytest -q {hidden_dir}"),
        "initial_repo": read_tree(HERE / spec["seed"]),
        "stages": [
            {"message": stage["message"],
             "requirements": [{"id": i, "text": t} for i, t in stage["requirements"]],
             "hidden_tests": read_tree(HERE / stage["hidden"])}
            for stage in spec["stages"]
        ],
    }


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for task_id, spec in TASKS.items():
        (OUT / f"{task_id}.json").write_text(
            json.dumps(build(task_id, spec), indent=2) + "\n")


if __name__ == "__main__":
    main()
