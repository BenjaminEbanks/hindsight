"""The notebook. Pages have a stable id; links are bound to ids when written.

pages/<slug>.md stays the human-facing file. meta.json holds what a file can't:
ids, each page's link bindings, its revisions, and who used to have which title.
A page file meta.json has never seen is a new page.
"""
from __future__ import annotations

import json
import os
import tempfile

from . import storage
from .index import SearchIndex
from .links import extract_links
from .models import Page, slugify


class PageExists(ValueError):
    """A page with that title (slug) is already there."""


class PageNotFound(KeyError):
    """No page has that title."""


class Notebook:
    def __init__(self, root: str) -> None:
        self.root = str(root)
        self.pages_dir = os.path.join(self.root, "pages")
        self.meta_path = os.path.join(self.root, "meta.json")
        os.makedirs(self.pages_dir, exist_ok=True)
        self._pages = storage.read_all(self.pages_dir)
        self._next_id = 1
        self._ids: dict[str, str] = {}       # slug -> id
        self._meta: dict[str, dict] = {}     # id -> {bindings, revisions}
        self._past: dict[str, list[str]] = {}  # old slug -> ids, latest last
        self._load_meta()
        self._index = SearchIndex()
        for slug, page in self._pages.items():
            self._index.add(slug, f"{page.title} {page.body}")
        self._adopt_disk()

    # -------------------------------------------------------- persistence
    def _load_meta(self) -> None:
        if os.path.exists(self.meta_path):
            with open(self.meta_path) as f:
                data = json.load(f)
            self._next_id = data["next_id"]
            self._ids = data["ids"]
            self._meta = data["meta"]
            self._past = data["past"]

    def _save_meta(self) -> None:
        fd, tmp = tempfile.mkstemp(dir=self.root, suffix=".tmp")
        with os.fdopen(fd, "w") as f:
            json.dump({"next_id": self._next_id, "ids": self._ids,
                       "meta": self._meta, "past": self._past}, f, indent=1)
        os.replace(tmp, self.meta_path)

    def _adopt_disk(self) -> None:
        """Reconcile with files people added, edited or removed by hand."""
        for slug in [s for s in self._ids if s not in self._pages]:
            self._forget(self._ids[slug])
            del self._ids[slug]
        for slug, page in self._pages.items():
            if slug not in self._ids:
                self._new_entry(slug, page)
            else:
                meta = self._meta[self._ids[slug]]
                if meta["revisions"][-1]["body"] != page.body:
                    self._rebind_for_body(self._ids[slug], page.body)
                    self._add_revision(self._ids[slug], page)
        self._bind_all()
        self._save_meta()

    def _new_entry(self, slug: str, page: Page) -> str:
        pid = str(self._next_id)
        self._next_id += 1
        self._ids[slug] = pid
        self._meta[pid] = {"bindings": {}, "revisions": []}
        self._add_revision(pid, page)
        return pid

    def _add_revision(self, pid: str, page: Page) -> None:
        meta = self._meta[pid]
        meta["revisions"].append({"rev": len(meta["revisions"]) + 1,
                                  "title": page.title, "body": page.body,
                                  "bindings": dict(meta["bindings"])})

    # ------------------------------------------------------------ binding
    def _lookup(self, slug: str) -> str | None:
        if slug in self._ids:
            return self._ids[slug]
        holders = self._past.get(slug)
        return holders[-1] if holders else None

    def _title_of(self, pid: str) -> str | None:
        for slug, other in self._ids.items():
            if other == pid:
                return self._pages[slug].title
        return None

    def _rebind_for_body(self, pid: str, body: str) -> None:
        wanted = {slugify(l) for l in extract_links(body)}
        meta = self._meta[pid]
        meta["bindings"] = {k: v for k, v in meta["bindings"].items()
                            if k in wanted}
        self._bind_page(pid, body)

    def _bind_page(self, pid: str, body: str) -> None:
        bindings = self._meta[pid]["bindings"]
        for link in extract_links(body):
            key = slugify(link)
            if key not in bindings:
                target = self._lookup(key)
                if target is not None:
                    bindings[key] = target

    def _bind_all(self) -> None:
        for slug, pid in self._ids.items():
            before = dict(self._meta[pid]["bindings"])
            self._bind_page(pid, self._pages[slug].body)
            if before != self._meta[pid]["bindings"]:
                self._meta[pid]["revisions"][-1]["bindings"] = dict(
                    self._meta[pid]["bindings"])

    def _forget(self, pid: str) -> None:
        del self._meta[pid]
        for holders in self._past.values():
            while pid in holders:
                holders.remove(pid)
        for meta in self._meta.values():
            meta["bindings"] = {k: v for k, v in meta["bindings"].items()
                                if v != pid}

    # ------------------------------------------------------------- reads
    def titles(self) -> list[str]:
        return sorted(p.title for p in self._pages.values())

    def has(self, title: str) -> bool:
        return slugify(title) in self._pages

    def get(self, title: str) -> Page:
        try:
            return self._pages[slugify(title)]
        except KeyError:
            raise PageNotFound(title) from None

    def links_from(self, title: str) -> list[str]:
        return extract_links(self.get(title).body)

    def target_of(self, title: str, link_text: str) -> str | None:
        self.get(title)
        pid = self._ids[slugify(title)]
        target = self._meta[pid]["bindings"].get(slugify(link_text))
        return self._title_of(target) if target else None

    def search(self, query: str) -> list[str]:
        return sorted(self._pages[s].title for s in self._index.search(query))

    def pages_with_tag(self, tag: str) -> list[str]:
        wanted = tag.lower()
        return sorted(p.title for p in self._pages.values()
                      if wanted in (t.lower() for t in p.tags))

    def history(self, title: str) -> list[dict]:
        self.get(title)
        revisions = self._meta[self._ids[slugify(title)]]["revisions"]
        return [{"rev": r["rev"], "title": r["title"], "body": r["body"]}
                for r in revisions]

    # ---------------------------------------------------------- mutations
    def _index_page(self, slug: str, page: Page) -> None:
        self._index.remove(slug)
        self._index.add(slug, f"{page.title} {page.body}")

    def create(self, title: str, body: str = "", tags=()) -> Page:
        slug = slugify(title)
        if not slug:
            raise ValueError("a page needs a title")
        if slug in self._pages:
            raise PageExists(title)
        page = Page(title, body, list(tags))
        self._pages[slug] = page
        storage.write_page(self.pages_dir, page)
        self._index_page(slug, page)
        pid = self._new_entry(slug, page)
        self._bind_all()
        self._meta[pid]["revisions"][-1]["bindings"] = dict(
            self._meta[pid]["bindings"])
        self._save_meta()
        return page

    def update(self, title: str, body: str) -> Page:
        page = self.get(title)
        slug = slugify(title)
        pid = self._ids[slug]
        page.body = body
        storage.write_page(self.pages_dir, page)
        self._index_page(slug, page)
        self._rebind_for_body(pid, body)
        self._add_revision(pid, page)
        self._save_meta()
        return page

    def rename(self, old: str, new: str) -> Page:
        page = self.get(old)
        old_slug, new_slug = slugify(old), slugify(new)
        if not new_slug:
            raise ValueError("a page needs a title")
        if new_slug != old_slug and new_slug in self._pages:
            raise PageExists(new)
        pid = self._ids[old_slug]
        storage.remove_page(self.pages_dir, page.title)
        self._index.remove(old_slug)
        del self._pages[old_slug], self._ids[old_slug]
        if new_slug != old_slug:
            holders = self._past.setdefault(old_slug, [])
            if pid in holders:
                holders.remove(pid)
            holders.append(pid)
        page.title = new
        self._pages[new_slug] = page
        self._ids[new_slug] = pid
        storage.write_page(self.pages_dir, page)
        self._index_page(new_slug, page)
        self._add_revision(pid, page)
        self._bind_all()
        self._save_meta()
        return page

    def revert(self, title: str, rev: int) -> Page:
        page = self.get(title)
        slug = slugify(title)
        pid = self._ids[slug]
        meta = self._meta[pid]
        if not 1 <= rev <= len(meta["revisions"]):
            raise ValueError(f"no revision {rev}")
        old = meta["revisions"][rev - 1]
        meta["bindings"] = {k: v for k, v in old["bindings"].items()
                            if v in self._meta}
        page.body = old["body"]
        self._bind_page(pid, page.body)
        storage.write_page(self.pages_dir, page)
        self._index_page(slug, page)
        self._add_revision(pid, page)
        self._save_meta()
        return page

    def delete(self, title: str) -> None:
        page = self.get(title)
        slug = slugify(title)
        storage.remove_page(self.pages_dir, page.title)
        self._index.remove(slug)
        del self._pages[slug]
        pid = self._ids.pop(slug)
        self._forget(pid)
        self._bind_all()
        self._save_meta()
