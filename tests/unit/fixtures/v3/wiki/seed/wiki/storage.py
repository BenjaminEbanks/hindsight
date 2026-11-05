"""On-disk format: pages/<slug>.md holding a two-line header, a blank line, the body."""
from __future__ import annotations

import os
import tempfile

from .models import Page, slugify


def render_page(page: Page) -> str:
    return (f"title: {page.title}\n"
            f"tags: {', '.join(page.tags)}\n"
            f"\n{page.body}")


def parse_page(text: str) -> Page:
    head, _, body = text.partition("\n\n")
    fields = {}
    for line in head.splitlines():
        key, _, value = line.partition(":")
        fields[key.strip()] = value.strip()
    tags = [t.strip() for t in fields.get("tags", "").split(",") if t.strip()]
    return Page(fields["title"], body, tags)


def page_path(pages_dir: str, title: str) -> str:
    return os.path.join(pages_dir, slugify(title) + ".md")


def write_page(pages_dir: str, page: Page) -> None:
    fd, tmp = tempfile.mkstemp(dir=pages_dir, suffix=".tmp")
    with os.fdopen(fd, "w") as f:
        f.write(render_page(page))
    os.replace(tmp, page_path(pages_dir, page.title))


def remove_page(pages_dir: str, title: str) -> None:
    os.remove(page_path(pages_dir, title))


def read_all(pages_dir: str) -> dict[str, Page]:
    pages = {}
    for name in sorted(os.listdir(pages_dir)):
        if name.endswith(".md"):
            with open(os.path.join(pages_dir, name)) as f:
                page = parse_page(f.read())
            pages[slugify(page.title)] = page
    return pages
