"""The local patch: an aliases.json mapping old slugs to current ones."""
from __future__ import annotations

import json
import os

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
        self.alias_path = os.path.join(self.root, "aliases.json")
        os.makedirs(self.pages_dir, exist_ok=True)
        self._pages = storage.read_all(self.pages_dir)
        self._aliases = {}
        if os.path.exists(self.alias_path):
            with open(self.alias_path) as f:
                self._aliases = json.load(f)
        self._index = SearchIndex()
        for slug, page in self._pages.items():
            self._index.add(slug, f"{page.title} {page.body}")

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
        slug = slugify(link_text)
        if slug not in self._pages:
            slug = self._aliases.get(slug, slug)
        target = self._pages.get(slug)
        return target.title if target else None

    def search(self, query: str) -> list[str]:
        return sorted(self._pages[s].title for s in self._index.search(query))

    def pages_with_tag(self, tag: str) -> list[str]:
        wanted = tag.lower()
        return sorted(p.title for p in self._pages.values()
                      if wanted in (t.lower() for t in p.tags))

    def create(self, title: str, body: str = "", tags=()) -> Page:
        slug = slugify(title)
        if not slug:
            raise ValueError("a page needs a title")
        if slug in self._pages:
            raise PageExists(title)
        page = Page(title, body, list(tags))
        self._pages[slug] = page
        storage.write_page(self.pages_dir, page)
        self._index.add(slug, f"{title} {body}")
        return page

    def update(self, title: str, body: str) -> Page:
        page = self.get(title)
        page.body = body
        storage.write_page(self.pages_dir, page)
        slug = slugify(title)
        self._index.remove(slug)
        self._index.add(slug, f"{page.title} {body}")
        return page

    def rename(self, old: str, new: str) -> Page:
        page = self.get(old)
        old_slug, new_slug = slugify(old), slugify(new)
        if new_slug != old_slug and new_slug in self._pages:
            raise PageExists(new)
        storage.remove_page(self.pages_dir, page.title)
        self._index.remove(old_slug)
        del self._pages[old_slug]
        page.title = new
        self._pages[new_slug] = page
        storage.write_page(self.pages_dir, page)
        self._index.add(new_slug, f"{new} {page.body}")
        for key, value in list(self._aliases.items()):
            if value == old_slug:
                self._aliases[key] = new_slug
        self._aliases[old_slug] = new_slug
        with open(self.alias_path, "w") as f:
            json.dump(self._aliases, f)
        return page

    def delete(self, title: str) -> None:
        page = self.get(title)
        storage.remove_page(self.pages_dir, page.title)
        slug = slugify(title)
        self._index.remove(slug)
        del self._pages[slug]
