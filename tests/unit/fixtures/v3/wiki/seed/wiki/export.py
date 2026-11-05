"""Static HTML export: <slug>.html per page plus an index.html."""
from __future__ import annotations

import html
import os

from .links import LINK
from .models import slugify
from .notebook import Notebook


def render_body(nb: Notebook, title: str) -> str:
    def replace(match) -> str:
        text = match.group(1)
        if nb.has(text):
            return f'<a href="{slugify(text)}.html">{html.escape(text)}</a>'
        return f'<span class="broken">{html.escape(text)}</span>'
    return LINK.sub(replace, nb.get(title).body)


def export_html(nb: Notebook, out_dir: str) -> list[str]:
    """Write the site; returns the page file names, sorted."""
    os.makedirs(out_dir, exist_ok=True)
    names = []
    for title in nb.titles():
        name = slugify(title) + ".html"
        names.append(name)
        with open(os.path.join(out_dir, name), "w") as f:
            f.write(f"<h1>{html.escape(title)}</h1>\n<p>{render_body(nb, title)}</p>\n")
    with open(os.path.join(out_dir, "index.html"), "w") as f:
        for title in nb.titles():
            f.write(f'<li><a href="{slugify(title)}.html">{html.escape(title)}</a></li>\n')
    return sorted(names)
