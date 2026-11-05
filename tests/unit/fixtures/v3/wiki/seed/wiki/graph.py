"""Who links to whom."""
from __future__ import annotations

from .links import extract_links
from .models import slugify
from .notebook import Notebook


def backlinks(nb: Notebook, title: str) -> list[str]:
    """Sorted titles of the pages linking to `title`."""
    slug = slugify(title)
    return sorted(t for t in nb.titles()
                  if any(slugify(l) == slug for l in extract_links(nb.get(t).body)))


def broken_links(nb: Notebook) -> dict[str, list[str]]:
    """Page title -> link texts that go nowhere; clean pages are left out."""
    out = {}
    for title in nb.titles():
        bad = [l for l in nb.links_from(title) if not nb.has(l)]
        if bad:
            out[title] = bad
    return out


def orphans(nb: Notebook) -> list[str]:
    """Sorted titles nothing else links to."""
    linked = set()
    for title in nb.titles():
        for link in nb.links_from(title):
            if slugify(link) != slugify(title):
                linked.add(slugify(link))
    return [t for t in nb.titles() if slugify(t) not in linked]
