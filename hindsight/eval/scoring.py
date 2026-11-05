"""Hidden-test scoring.

Hidden tests run ONLY at scoring time, in a temp directory inside the
repo, then are deleted — they never ship to the main agent or any worker
box during an episode. The result is parsed from JUnit XML when the task
uses pytest, else from the exit code; either way the outcome is the real
executed result (never fake results).
"""

from __future__ import annotations

import ast
import shutil
import subprocess
import tempfile
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

from .tasks import Task

_HIDDEN_DIR_NAME = ".hindsight_eval_hidden"
_TIMEOUT_SECONDS = 900


@dataclass
class HiddenTestResult:
    passed: int
    failed: int
    output: str = ""
    exit_code: int | None = None
    ran: bool = True
    error: str = ""
    # Per-test record: [{name, outcome, message}], outcome in
    # passed/failed/collection_error. A collection error is ONE junit case
    # per file however many tests the file holds, so `failed` alone is not
    # comparable across episodes (the same task scored 0p/2f and 0p/4f);
    # compare `passed` against `expected_tests` instead.
    tests: list[dict] = field(default_factory=list)
    expected_tests: int = 0

    @property
    def collection_errors(self) -> int:
        return sum(1 for t in self.tests if t["outcome"] == "collection_error")

    @property
    def ok(self) -> bool:
        return self.ran and self.failed == 0 and self.passed > 0


def _clean_env() -> dict[str, str]:
    """Hidden tests must score ONLY the staged repo. Inherited venv state
    (VIRTUAL_ENV / UV_* from an outer `uv run` project) makes the inner
    `uv run --no-project` layer on the outer venv, whose editable install
    puts the outer project root on sys.path — `import counter` then
    resolves to a file OUTSIDE the repo and a broken design can score as
    passing (found live)."""
    import os
    return {k: v for k, v in os.environ.items()
            if not k.startswith("UV_") and k != "VIRTUAL_ENV"
            and k != "PYTHONPATH"}


def count_tests(sources: dict[str, str]) -> int:
    """Test functions in the hidden-test sources (AST, no execution)."""
    n = 0
    for code in sources.values():
        n += sum(1 for node in ast.walk(ast.parse(code))
                 if isinstance(node, ast.FunctionDef)
                 and node.name.startswith("test_"))
    return n


def run_hidden_tests(task: Task, repo_dir: str | Path,
                     timeout: int = _TIMEOUT_SECONDS) -> HiddenTestResult:
    """Copy the task's hidden tests into the repo, run them, clean up.

    Paths are resolved to absolute: a relative repo_dir (plus a relative
    --junitxml) made pytest write its report to a doubled path inside the
    repo (found live)."""
    repo_dir = Path(repo_dir).resolve()
    hidden_dir = repo_dir / _HIDDEN_DIR_NAME
    if hidden_dir.exists():
        shutil.rmtree(hidden_dir)
    hidden_dir.mkdir(parents=True)
    tests = task.hidden_tests()
    if not tests:
        shutil.rmtree(hidden_dir)
        return HiddenTestResult(0, 0, ran=False,
                                error="task has no hidden tests")
    for rel, code in tests.items():
        f = hidden_dir / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(code)

    cmd = task.hidden_test_command_for(_HIDDEN_DIR_NAME)
    junit_path = hidden_dir / "junit.xml"
    if "pytest" in cmd:
        cmd = f"{cmd} --junitxml={junit_path} -p no:cacheprovider"
    try:
        r = subprocess.run(cmd, shell=True, cwd=str(repo_dir),
                           capture_output=True, text=True, timeout=timeout,
                           env=_clean_env())
        out = (r.stdout or "") + (r.stderr or "")
        result = _parse(r.returncode, out, junit_path)
    except subprocess.TimeoutExpired:
        result = HiddenTestResult(0, 0, ran=False, error="hidden tests timed out")
    finally:
        shutil.rmtree(hidden_dir, ignore_errors=True)
    result.expected_tests = count_tests(tests)
    return result


def _parse(exit_code: int, output: str,
           junit_path: Path) -> HiddenTestResult:
    if junit_path.exists():
        try:
            tree = ET.parse(junit_path)
            passed = failed = 0
            tests: list[dict] = []
            for tc in tree.iter("testcase"):
                bad = [el for el in list(tc) if el.tag in ("failure", "error")]
                if bad:
                    failed += 1
                else:
                    passed += 1
                message = (bad[0].get("message") or "")[:500] if bad else ""
                if not bad:
                    outcome = "passed"
                elif "collection failure" in message:
                    outcome = "collection_error"
                else:
                    outcome = "failed"
                tests.append({"name": tc.get("name") or "", "outcome": outcome,
                              "message": message})
            return HiddenTestResult(passed, failed, output[-8000:], exit_code,
                                    tests=tests)
        except ET.ParseError:
            pass  # fall through to exit-code-only evidence
    # Coarse fallback: exit code only (the command is not pytest-based).
    return HiddenTestResult(0, 0 if exit_code == 0 else 1, output[-8000:],
                            exit_code)
