"""Contract audit for eval tasks (2026-11-02, after the 3rd task-clarity
bug of the night: bookmarks' hidden tests demanded (title, url) tuples
and class-callable load() — neither stated anywhere in the spec).

The rule this enforces: SPEC = TESTED CONTRACT. Every symbol or shape
the hidden tests depend on must be derivable from the task's stage
messages/requirements; otherwise episodes fail for reasons orthogonal
to the design-vs-hindsight question (the ExLabel/SWE-TQ task-clarity
failure class, arXiv:2610.39417).

What it extracts from the hidden tests (AST, no execution):
  1. names imported FROM the project module (classes/functions the
     repo must define)  -> must appear in spec text
  2. methods called on a CLASS name (X.method(...)) -> spec must state
     the class-callable form (e.g. "BookmarkStore.load(path)")
  3. return values compared against tuple/list literals -> spec must
     state the return shape for that method

This is a syntactic floor, not proof of fairness: it cannot see semantic
gaps (meetings' next_dates anchor) or a test that hardcodes something the
spec leaves open (bookmarks' file format). Review each assertion by hand too.

Exit code 1 if any task fails the audit (CI-able).

Usage: uv run python scripts/audit_contracts.py [tasks_dir]
"""
from __future__ import annotations

import ast
import glob
import json
import os
import re
import sys

WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def spec_text(task: dict) -> str:
    """Only what the agent is told. `requirements` is ledger ground truth and
    never reaches the agent: counting it let meetings pass this audit while
    its list() return shape was unstated."""
    return " ".join(s.get("message") or "" for s in task["stages"])


def project_module(task: dict) -> str:
    """The module the task asks the agent to create ('expenses.py: ...')."""
    m = re.search(r"(\w+)\.py", task["stages"][0].get("message") or "")
    return m.group(1) if m else ""


def audit_task(path: str) -> list[str]:
    task = json.load(open(path))
    tid = task.get("id") or os.path.basename(path)[:-5]
    text = spec_text(task)
    mod = project_module(task)
    problems: list[str] = []

    for stage in task["stages"]:
        for fname, body in (stage.get("hidden_tests") or {}).items():
            try:
                tree = ast.parse(body)
            except SyntaxError as e:
                problems.append(f"{tid}/{fname}: hidden test doesn't parse: {e}")
                continue

            # 1. imports from the project module
            for node in ast.walk(tree):
                if (isinstance(node, ast.ImportFrom) and node.module == mod):
                    for a in node.names:
                        if a.name not in text:
                            problems.append(
                                f"{tid}/{fname}: hidden tests import "
                                f"'{a.name}' from {mod} but the spec never "
                                f"names it (the expenses/meetings bug class)")

            # 2. calls on a class name: ClassName.method(...)
            classes = {n.id for n in ast.walk(tree)
                       if isinstance(n, ast.Name) and n.id[:1].isupper()}
            for node in ast.walk(tree):
                if (isinstance(node, ast.Call)
                        and isinstance(node.func, ast.Attribute)
                        and isinstance(node.func.value, ast.Name)
                        and node.func.value.id in classes):
                    callee = f"{node.func.value.id}.{node.func.attr}"
                    if callee not in text:
                        problems.append(
                            f"{tid}/{fname}: hidden tests call "
                            f"{callee}(...) on the class but the spec never "
                            f"shows that form (the bookmarks load() bug)")

            # 3. return values compared to tuple/list literals
            for node in ast.walk(tree):
                if (isinstance(node, ast.Compare)
                        and isinstance(node.left, ast.Call)
                        and isinstance(node.left.func, ast.Attribute)
                        and any(isinstance(c, (ast.Tuple, ast.List))
                                for c in node.comparators)
                        and any(isinstance(elt, (ast.Tuple, ast.List))
                               for c in node.comparators for elt in c.elts)):
                    method = node.left.func.attr
                    if not re.search(
                            rf"{method}\(\)[^.;]*"
                            rf"(tupl|tuple|\(title|\(name|pair)",
                            text, re.I):
                        problems.append(
                            f"{tid}/{fname}: hidden tests compare "
                            f"{method}() to tuple/list literals but the "
                            f"spec never states its return shape "
                            f"(the bookmarks entries() bug)")

    return problems


def main(tasks_dir: str) -> int:
    bad = 0
    for p in sorted(glob.glob(os.path.join(tasks_dir, "*.json"))):
        problems = audit_task(p)
        tid = json.load(open(p)).get("id") or os.path.basename(p)[:-5]
        if problems:
            bad += 1
            print(f"FAIL {tid}")
            for pr in problems:
                print(f"  - {pr}")
        else:
            print(f"ok   {tid}")
    print("\nverdict:", "FAIL" if bad else
          "all task specs state their tested contracts")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "eval/tasks/v2"))
