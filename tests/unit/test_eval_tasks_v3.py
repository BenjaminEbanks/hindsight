"""v3 lock-in task discrimination tests.

v2's empty repos let a late requirement cost nothing: the agent rewrites. v3
seeds working repos with persisted data and internal consumers. The bar:

  - the seeded repo's own tests pass;
  - hidden tests pass on the reference final design;
  - hidden tests fail on the seed and on a plausible local patch of it;
  - the control's reference only adds lines to the seed, so nothing in it
    calls for a redesign.

Reference designs, local patches and hidden tests are files under
tests/unit/fixtures/v3/; build_tasks.py there generates eval/tasks/v3/.
"""

from __future__ import annotations

import ast
import difflib
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

from hindsight.eval.scoring import run_hidden_tests
from hindsight.eval.tasks import Task

TASKS_DIR = Path("eval/tasks/v3")
FIXTURES = Path("tests/unit/fixtures/v3")
TASK_IDS = ["warehouse_locations_lots", "wiki_stable_identity",
            "warehouse_reports_control"]
REFERENCE = {"warehouse_locations_lots": "warehouse/reference",
             "wiki_stable_identity": "wiki/reference",
             "warehouse_reports_control": "warehouse_control/reference"}
LOCAL_PATCH = {"warehouse_locations_lots": "warehouse/local_patch",
               "wiki_stable_identity": "wiki/local_patch"}
PACKAGE = {"warehouse_locations_lots": "stockroom",
           "wiki_stable_identity": "wiki",
           "warehouse_reports_control": "stockroom"}


def load(task_id: str) -> Task:
    return Task.load(TASKS_DIR / f"{task_id}.json")


def materialize(task_id: str, dest: Path, overlay: str | None = None) -> Path:
    load(task_id).materialize(dest)
    if overlay:
        root = FIXTURES / overlay
        for path in root.rglob("*.py"):
            target = dest / path.relative_to(root)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(path.read_text())
    return dest


def seed_tests_pass(repo: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests"],
        cwd=repo, capture_output=True, text=True, timeout=120)


def by_stage(task_id: str, repo: Path) -> list:
    """Hidden results for each stage alone, so a partial design shows up."""
    raw = json.loads((TASKS_DIR / f"{task_id}.json").read_text())
    results = []
    for stage in raw["stages"]:
        path = repo.parent / f"{task_id}.json"
        path.write_text(json.dumps({**raw, "stages": [stage]}))
        results.append(run_hidden_tests(Task.load(path), repo))
    return results


def test_all_v3_tasks_load_and_control_is_marked():
    tasks = Task.load_dir(TASKS_DIR)
    assert sorted(t.id for t in tasks) == sorted(TASK_IDS)
    assert [t.id for t in tasks if t.no_change] == ["warehouse_reports_control"]
    for task in tasks:
        assert 3 <= len(task.stages) <= 4


def test_json_is_in_sync_with_the_fixtures():
    spec = importlib.util.spec_from_file_location(
        "build_v3_tasks", FIXTURES / "build_tasks.py")
    build_tasks = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(build_tasks)
    for task_id, task_spec in build_tasks.TASKS.items():
        on_disk = json.loads((TASKS_DIR / f"{task_id}.json").read_text())
        assert on_disk == build_tasks.build(task_id, task_spec)


@pytest.mark.parametrize("task_id", TASK_IDS)
def test_seed_size_is_in_the_lock_in_range(task_id):
    repo = load(task_id).initial_repo
    lines = sum(body.count("\n") for path, body in repo.items()
                if path.endswith(".py"))
    assert 500 <= lines <= 1500, lines


@pytest.mark.parametrize("task_id", TASK_IDS)
def test_seed_repo_own_tests_pass(tmp_path, task_id):
    proc = seed_tests_pass(materialize(task_id, tmp_path / "repo"))
    assert proc.returncode == 0, proc.stdout + proc.stderr


@pytest.mark.parametrize("task_id", TASK_IDS)
def test_reference_keeps_the_seed_tests_passing(tmp_path, task_id):
    repo = materialize(task_id, tmp_path / "repo", REFERENCE[task_id])
    proc = seed_tests_pass(repo)
    assert proc.returncode == 0, proc.stdout + proc.stderr


@pytest.mark.parametrize("task_id", TASK_IDS)
def test_hidden_tests_pass_on_reference(tmp_path, task_id):
    repo = materialize(task_id, tmp_path / "repo", REFERENCE[task_id])
    result = run_hidden_tests(load(task_id), repo)
    assert result.ran, result.error
    assert result.failed == 0, result.output
    assert result.passed > 0


@pytest.mark.parametrize("task_id", TASK_IDS)
def test_hidden_tests_fail_on_seed(tmp_path, task_id):
    result = run_hidden_tests(load(task_id), materialize(task_id, tmp_path / "repo"))
    assert result.ran, result.error
    assert result.failed > 0


@pytest.mark.parametrize("task_id", sorted(LOCAL_PATCH))
def test_local_patch_keeps_seed_tests_but_fails_hidden(tmp_path, task_id):
    repo = materialize(task_id, tmp_path / "repo", LOCAL_PATCH[task_id])
    proc = seed_tests_pass(repo)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    result = run_hidden_tests(load(task_id), repo)
    assert result.ran, result.error
    assert result.failed > 0
    assert result.passed > 0, "a local patch should pass the obvious cases"


def test_wiki_alias_patch_passes_rename_and_breaks_on_title_reuse(tmp_path):
    """The break is the point of the task: stage 2 looks finished with aliases."""
    repo = materialize("wiki_stable_identity", tmp_path / "repo",
                       LOCAL_PATCH["wiki_stable_identity"])
    tags, rename, reuse, history = by_stage("wiki_stable_identity", repo)
    assert tags.failed == 0 and tags.passed > 0
    assert rename.failed == 0 and rename.passed > 0
    assert reuse.failed > 0
    assert history.failed > 0


def test_control_reference_only_adds_lines_to_the_seed():
    seed = load("warehouse_reports_control").initial_repo
    root = FIXTURES / REFERENCE["warehouse_reports_control"]
    for path in root.rglob("*.py"):
        rel = path.relative_to(root).as_posix()
        diff = difflib.ndiff(seed[rel].splitlines(), path.read_text().splitlines())
        removed = [line for line in diff if line.startswith("- ")]
        assert removed == [], (rel, removed)


@pytest.mark.parametrize("task_id", TASK_IDS)
def test_hidden_imports_exist_in_seed_or_are_stated(task_id):
    """Stricter than audit_contracts' check 1, which only knows flat modules."""
    task = load(task_id)
    package = PACKAGE[task_id]
    messages = " ".join(s.message for s in task.stages)
    seed = "\n".join(task.initial_repo.values())
    for name, code in task.hidden_tests().items():
        for node in ast.walk(ast.parse(code)):
            if isinstance(node, ast.ImportFrom) and (node.module or "").startswith(package):
                for alias in node.names:
                    assert alias.name in seed or alias.name in messages, (name, alias.name)


@pytest.mark.parametrize("task_id", TASK_IDS)
def test_hidden_tests_touch_no_clock_or_third_party(task_id):
    allowed = {"csv", "io", "json", "os", "pytest", PACKAGE[task_id]}
    for name, code in load(task_id).hidden_tests().items():
        for node in ast.walk(ast.parse(code)):
            mods = []
            if isinstance(node, ast.Import):
                mods = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                mods = [node.module or ""]
            for mod in mods:
                assert mod.split(".")[0] in allowed, (name, mod)
