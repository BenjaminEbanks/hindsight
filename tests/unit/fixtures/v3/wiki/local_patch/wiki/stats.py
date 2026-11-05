"""Numbers for the monthly wiki-health note."""
from __future__ import annotations

from .index import words
from .notebook import Notebook


def word_count(nb: Notebook, title: str) -> int:
    return len(nb.get(title).body.split())


def total_words(nb: Notebook) -> int:
    return sum(word_count(nb, t) for t in nb.titles())


def most_linked(nb: Notebook, n: int = 5) -> list[tuple[str, int]]:
    counts: dict[str, int] = {}
    for title in nb.titles():
        targets = {nb.target_of(title, l) for l in nb.links_from(title)}
        for target in targets - {None}:
            counts[target] = counts.get(target, 0) + 1
    ranked = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    return ranked[:n]


def vocabulary(nb: Notebook) -> int:
    seen: set[str] = set()
    for title in nb.titles():
        seen |= words(nb.get(title).body)
    return len(seen)


def tagless(nb: Notebook) -> list[str]:
    return [t for t in nb.titles() if not nb.get(t).tags]
