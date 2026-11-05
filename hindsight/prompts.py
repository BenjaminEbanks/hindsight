"""Prompt templates. Versioned files live in hindsight/prompts/;
this module renders them, replacing {braces} tokens only (JSON braces untouched)."""

from __future__ import annotations

import re
from pathlib import Path

TEMPLATE_VERSIONS = {
    "design_review": "1",
    "design_directions": "2",
    "worker_preamble_audit": "6",
    "council_critique": "5",
    "council_chairman": "5",
    "delivery_nudge_audit": "2",
}

_PROMPT_DIR = Path(__file__).parent / "prompts"

_TOKEN = re.compile(r"\{([a-z_][a-z0-9_]*)\}")


def render(template: str, **vars: str) -> str:
    def sub(m: re.Match) -> str:
        return str(vars.get(m.group(1), m.group(0)))

    return _TOKEN.sub(sub, template)


def load(name: str) -> str:
    return (_PROMPT_DIR / f"{name}.txt").read_text()
