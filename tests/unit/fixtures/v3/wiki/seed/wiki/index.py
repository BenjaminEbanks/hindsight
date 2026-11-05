"""A small inverted index for search, keyed by slug."""
from __future__ import annotations

import re

WORD = re.compile(r"[a-z0-9]+")


def words(text: str) -> set[str]:
    return set(WORD.findall(text.lower()))


class SearchIndex:
    def __init__(self) -> None:
        self._postings: dict[str, set[str]] = {}

    def add(self, slug: str, text: str) -> None:
        for word in words(text):
            self._postings.setdefault(word, set()).add(slug)

    def remove(self, slug: str) -> None:
        for slugs in self._postings.values():
            slugs.discard(slug)

    def search(self, query: str) -> set[str]:
        """Slugs containing every word of the query."""
        terms = words(query)
        if not terms:
            return set()
        hits = [self._postings.get(t, set()) for t in terms]
        return set.intersection(*hits)
