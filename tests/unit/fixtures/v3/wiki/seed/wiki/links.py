"""[[Page Title]] links in page bodies."""
from __future__ import annotations

import re

LINK = re.compile(r"\[\[([^\[\]]+)\]\]")


def extract_links(body: str) -> list[str]:
    """Distinct link texts, in the order they first appear."""
    seen, out = set(), []
    for text in LINK.findall(body):
        if text not in seen:
            seen.add(text)
            out.append(text)
    return out
