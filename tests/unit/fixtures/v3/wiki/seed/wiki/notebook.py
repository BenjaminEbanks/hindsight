"""The notebook: pages on disk, kept in memory, with a search index."""
from __future__ import annotations

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
        os.makedirs(self.pages_dir, exist_ok=True)
        self._pages = storage.read_all(self.pages_dir)
        self._index = SearchIndex()
        for slug, page in self._pages.items():
            self._index.add(slug, f"{page.title} {page.body}")

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
        """Current title of the page a link on `title` points to, or None."""
        self.get(title)
        target = self._pages.get(slugify(link_text))
        return target.title if target else None

    def search(self, query: str) -> list[str]:
        """Titles of pages whose title or body has every query word, sorted."""
        return sorted(self._pages[s].title for s in self._index.search(query))

    # ---------------------------------------------------------- mutations
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

    def delete(self, title: str) -> None:
        page = self.get(title)
        storage.remove_page(self.pages_dir, page.title)
        slug = slugify(title)
        self._index.remove(slug)
        del self._pages[slug]
