"""The evidence a run has. Workers check the code against the user's VERBATIM
messages and there is no demonstrably failing test, so this module owns what
replaces that evidence:

  - stage 0: `check_integrity` still finds everything. A changed test is excused
    only when the worker lists it with a reason, and any quote it gives must be
    the user's exact words. The change is then shown to the main agent to judge.
    Skip/xfail/`assert True`/selection-config findings are never excused;
  - edits to assertions inside an existing test body, which the regex gate
    cannot see, and which a worker could otherwise use to pass the final check.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Optional

from .gate.integrity import check_integrity

# Findings a user's quote can excuse: changes to what an existing test asserts.
_EXCUSABLE_KINDS = {"deleted_test", "edited_assert"}
# Below this a "quote" matches nearly any message ("the", "fix it").
_MIN_QUOTE_WORDS = 3

_ANY_DEF_RE = re.compile(r"^\s*(?:async\s+)?def\s+(\w+)")
_TEST_NAME_RE = re.compile(r"\btest_\w+")
_ASSERTION_RE = re.compile(r"\bassert|\braises\b")


# ------------------------------------------------------------------ messages

def render_user_messages(messages: list[str]) -> str:
    """The user's words, in order, untruncated and unparaphrased."""
    if not messages:
        return "(no user messages recorded)"
    return "\n\n".join(
        f"--- USER MESSAGE {i} ---\n{text}"
        for i, text in enumerate(messages, start=1))


# ----------------------------------------------------------------- integrity

@dataclass
class AuditIntegrity:
    passed: bool
    findings: list[dict] = field(default_factory=list)  # what still blocks
    allowed: list[dict] = field(default_factory=list)
    denied: list[dict] = field(default_factory=list)


def _is_test_file(path: str) -> bool:
    base = path.rsplit("/", 1)[-1]
    return base.endswith(".py") and (base.startswith("test_")
                                     or base.endswith("_test.py"))


def edited_assert_findings(patch: str) -> list[dict]:
    """Assertions removed or replaced inside an existing test file. Every
    finding carries the enclosing test's name when the diff shows it; an
    unattributable edit has test=None and can never be excused."""
    findings: list[dict] = []
    path: Optional[str] = None
    in_hunk = False
    current: Optional[str] = None

    def enter(code: str) -> None:
        nonlocal current
        m = _ANY_DEF_RE.match(code)
        if m:
            current = m.group(1) if m.group(1).startswith("test_") else None

    for line in patch.splitlines():
        if line.startswith("diff --git "):
            path, in_hunk, current = None, False, None
            m = re.match(r"diff --git a/\S+ b/(\S+)$", line)
            if m and _is_test_file(m.group(1)):
                path = m.group(1)
            continue
        if path is None:
            continue
        if line.startswith("@@"):
            in_hunk, current = True, None
            enter(line.split("@@", 2)[-1].strip())
            continue
        if not in_hunk or not line:
            continue
        code = line[1:]
        enter(code)
        if line[0] == "-" and _ASSERTION_RE.search(code):
            findings.append({"kind": "edited_assert", "file": path,
                             "line": code.strip()[:120], "test": current})
    return findings


def _finding_test(finding: dict) -> Optional[str]:
    if finding.get("test"):
        return finding["test"]
    m = _TEST_NAME_RE.search(finding.get("line") or "")
    return m.group(0) if m else None


def _normalize(text: str) -> str:
    return " ".join(text.split()).casefold()


def _quote_source(quote: str, user_messages: list[str]) -> Optional[int]:
    """1-based index of the user message containing the quote, or None."""
    needle = _normalize(quote)
    if len(needle.split()) < _MIN_QUOTE_WORDS:
        return None
    for i, message in enumerate(user_messages, start=1):
        if needle in _normalize(message):
            return i
    return None


def _changes_by_test(summary: Any) -> dict[str, dict]:
    """Test name -> tests_changed entry. Worker JSON is untrusted: anything
    malformed is simply not an excuse."""
    raw = summary.get("tests_changed") if isinstance(summary, dict) else None
    out: dict[str, dict] = {}
    for entry in raw if isinstance(raw, list) else []:
        if not isinstance(entry, dict):
            continue
        names = _TEST_NAME_RE.findall(str(entry.get("test") or ""))
        if names:
            out[names[-1]] = entry
    return out


def check_audit_integrity(patch: str, summary: Any, user_messages: list[str],
                          *, superseded: Optional[set[str]] = None) -> AuditIntegrity:
    base = check_integrity(patch, superseded_requirements=superseded)
    changes = _changes_by_test(summary)
    result = AuditIntegrity(passed=True)
    for finding in base.findings + edited_assert_findings(patch):
        if finding["kind"] not in _EXCUSABLE_KINDS:
            result.findings.append(finding)
            continue
        test = _finding_test(finding)
        entry = changes.get(test) if test else None
        quote = str((entry or {}).get("user_quote") or "")
        reason = str((entry or {}).get("reason") or "").strip()
        source = _quote_source(quote, user_messages) if quote else None
        allowed = bool(reason) and (not quote or source is not None)
        record = {"test": test, "kind": finding["kind"],
                  "file": finding.get("file"), "reason": reason,
                  "user_quote": quote}
        if not allowed:
            record["why_denied"] = (
                "not listed in tests_changed" if entry is None else
                "no reason given" if not reason else
                "user_quote is not verbatim from a user message")
            result.denied.append(record)
            result.findings.append(finding)
        else:
            if source is not None:
                record["user_message"] = source
            result.allowed.append(record)
    result.passed = not result.findings
    return result
