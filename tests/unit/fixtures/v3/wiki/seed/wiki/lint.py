"""House style checks, run before the monthly clean-up."""
from __future__ import annotations

from dataclasses import dataclass

from .notebook import Notebook

MAX_LINE = 120


@dataclass(frozen=True)
class Finding:
    title: str
    rule: str
    detail: str = ""


def _check_empty(nb: Notebook, title: str) -> list[Finding]:
    if not nb.get(title).body.strip():
        return [Finding(title, "empty")]
    return []


def _check_tags(nb: Notebook, title: str) -> list[Finding]:
    if not nb.get(title).tags:
        return [Finding(title, "untagged")]
    return []


def _check_lines(nb: Notebook, title: str) -> list[Finding]:
    out = []
    for number, line in enumerate(nb.get(title).body.splitlines(), start=1):
        if len(line) > MAX_LINE:
            out.append(Finding(title, "long-line", f"line {number}"))
    return out


def _check_title(nb: Notebook, title: str) -> list[Finding]:
    if title != title.strip() or title[:1].islower():
        return [Finding(title, "title-style")]
    return []


CHECKS = [_check_empty, _check_tags, _check_lines, _check_title]


def lint(nb: Notebook) -> list[Finding]:
    """Everything the checks found, page by page in title order."""
    findings = []
    for title in nb.titles():
        for check in CHECKS:
            findings.extend(check(nb, title))
    return findings


def summary(findings: list[Finding]) -> dict[str, int]:
    out: dict[str, int] = {}
    for finding in findings:
        out[finding.rule] = out.get(finding.rule, 0) + 1
    return out
