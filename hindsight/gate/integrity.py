"""Gate stage 0: integrity.

Static checks on the proposal patch. Regression and rebase checks live in
selection.py.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

TEST_WEAKENING_PATTERNS: list[tuple[str, str]] = [
    ("deleted_test", r"^-def test_"),
    ("deleted_test", r"^-async def test_"),
    ("skip_marker", r"^\+.*@pytest\.mark\.skip"),
    ("xfail_marker", r"^\+.*@pytest\.mark\.xfail"),
    ("skipif_marker", r"^\+.*@pytest\.mark\.skipif"),
    ("assert_true", r"^\+.*assert True"),
    ("unittest_skip", r"^\+.*@unittest\.skip"),
]

# test-selection changes in config (pytest.ini / pyproject / setup.cfg)
SELECTION_CONFIG_PATTERNS: list[tuple[str, str]] = [
    ("addopts_selection", r"^\+.*addopts\s*=.*( -k |--deselect)"),
    ("pytest_k", r"^\+.*-k\s+\S+"),
]

# Files whose diffs may indicate test-selection or dependency changes.
CONFIG_FILES = {"pytest.ini", "setup.cfg", "pyproject.toml", "tox.ini", "conftest.py"}
DEPENDENCY_FILES = {"requirements.txt", "pyproject.toml", "setup.py", "Pipfile", "package.json"}


@dataclass
class IntegrityResult:
    passed: bool
    findings: list[dict] = field(default_factory=list)


def check_integrity(patch: str, forbidden_deps: list[str] | None = None,
                    superseded_requirements: set[str] | None = None) -> IntegrityResult:
    """Stage 0: reject patches that weaken tests or violate constraints.

    `superseded_requirements`: requirement IDs the ledger marks superseded;
    a justified test change must reference one (checked against the worker's
    summary by the caller — here we only flag the patterns).
    """
    findings: list[dict] = []
    forbidden_deps = forbidden_deps or []

    current_file = None
    for line in patch.splitlines():
        if line.startswith(("+++ b/", "--- a/")):
            current_file = line[6:] if line.startswith("+++") else current_file
            continue
        if line.startswith("+") and not line.startswith("+++"):
            body = line[1:]
            for kind, pattern in TEST_WEAKENING_PATTERNS:
                if re.search(pattern, f"+{body}", re.MULTILINE):
                    findings.append({"kind": kind, "file": current_file, "line": body.strip()[:120]})
                    break
            if current_file in CONFIG_FILES:
                for kind, pattern in SELECTION_CONFIG_PATTERNS:
                    if re.search(pattern, f"+{body}"):
                        findings.append({"kind": kind, "file": current_file,
                                         "line": body.strip()[:120]})
            for dep in forbidden_deps:
                if re.search(rf"^\s*{re.escape(dep)}\b", body):
                    findings.append({"kind": "forbidden_dependency", "file": current_file,
                                     "line": body.strip()[:120]})
        elif line.startswith("-") and not line.startswith("---"):
            # deletions: removed test functions are also test weakening
            body = line[1:]
            if re.search(r"^(async )?def test_", body):
                findings.append({"kind": "deleted_test", "file": current_file,
                                 "line": body.strip()[:120]})

    return IntegrityResult(passed=not findings, findings=findings)
