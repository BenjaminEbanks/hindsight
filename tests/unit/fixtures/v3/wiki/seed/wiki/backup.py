"""Whole-notebook backups as JSON. Ops keeps years of these in cold storage."""
from __future__ import annotations

import json
import os

from . import storage
from .models import Page
from .notebook import Notebook


def dump(nb: Notebook) -> dict:
    """A JSON-serializable copy of the notebook."""
    pages = []
    for title in nb.titles():
        page = nb.get(title)
        pages.append({"title": page.title, "body": page.body, "tags": list(page.tags)})
    return {"pages": pages}


def restore(root: str, data: dict) -> Notebook:
    """Rebuild a notebook in an empty folder and open it."""
    pages_dir = os.path.join(str(root), "pages")
    os.makedirs(pages_dir, exist_ok=True)
    if os.listdir(pages_dir):
        raise ValueError(f"{root} already has pages")
    for raw in data["pages"]:
        storage.write_page(pages_dir, Page(raw["title"], raw["body"], list(raw["tags"])))
    return Notebook(str(root))


def save_backup(nb: Notebook, path: str) -> None:
    with open(path, "w") as f:
        json.dump(dump(nb), f, indent=1, sort_keys=True)


def load_backup(path: str) -> dict:
    with open(path) as f:
        return json.load(f)
