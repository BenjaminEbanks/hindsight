"""Who links to whom, following each link's bound target."""
from __future__ import annotations

from .notebook import Notebook


def backlinks(nb: Notebook, title: str) -> list[str]:
    """Sorted titles of the pages linking to `title`."""
    if not nb.has(title):
        return []
    current = nb.get(title).title
    return sorted(t for t in nb.titles()
                  if any(nb.target_of(t, l) == current for l in nb.links_from(t)))


def broken_links(nb: Notebook) -> dict[str, list[str]]:
    """Page title -> link texts that go nowhere; clean pages are left out."""
    out = {}
    for title in nb.titles():
        bad = [l for l in nb.links_from(title) if nb.target_of(title, l) is None]
        if bad:
            out[title] = bad
    return out


def orphans(nb: Notebook) -> list[str]:
    """Sorted titles nothing else links to."""
    linked = set()
    for title in nb.titles():
        for link in nb.links_from(title):
            target = nb.target_of(title, link)
            if target is not None and target != title:
                linked.add(target)
    return [t for t in nb.titles() if t not in linked]


def tag_counts(nb: Notebook) -> dict[str, int]:
    """Lowercased tag -> number of pages carrying it."""
    counts: dict[str, int] = {}
    for title in nb.titles():
        for tag in {t.lower() for t in nb.get(title).tags}:
            counts[tag] = counts.get(tag, 0) + 1
    return counts
