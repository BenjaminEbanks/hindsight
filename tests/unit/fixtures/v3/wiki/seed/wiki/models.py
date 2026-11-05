"""The page record and the title -> slug rule."""
from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass
class Page:
    title: str
    body: str = ""
    tags: list[str] = field(default_factory=list)


def slugify(title: str) -> str:
    """A page is identified by the slug of its title: 'Home Page' -> 'home-page'."""
    return re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
