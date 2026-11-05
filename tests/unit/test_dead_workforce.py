"""Dead-workforce honesty guards.

Root cause found live in the swarm eval: the default worker harness
pointed at a `pi` CLI that was NEVER installed in env boxes — every
worker died at Popen(["pi", ...]) with exit_reason=error, and the
pipeline laundered the resulting empty patches (only the harness's own
untracked hs_edge test files swept up by `git add -A`) into "delivered
redesigns": 28/28 examined proposals tests-only, 357/357 workers counted
"productive", zero requirement flips all night.

These tests pin the three guards that make that failure class loud:
  1. Executor._collect refuses any worker whose driver did not finish
     (exit_reason != "done") — no proposal, FAILED state, honest reason.
  2. Selector qualification requires a patch that touches product code
     (a tests-only patch is the signature of a worker that changed
     nothing).
  3. _touches_product_code parses both git and unified diff formats.

All Sail/model dependencies are injected fakes (UNIT TEST MOCKS).
"""

from __future__ import annotations

import json

import pytest

from hindsight.config import HindsightConfig
from hindsight.executor import Executor
from hindsight.selection import _touches_product_code
from hindsight.sail_client import SailClientError
from hindsight.store import Store

from test_selection import (  # reuse the selection fixture harness
    SelectionSail, make_selector, seed_run)


@pytest.fixture()
def parts(tmp_path):
    """Same shape as test_selection.parts (a usable snapshot + store)."""
    from hindsight.config import DEFAULT_SNAPSHOT_EXCLUDES
    from hindsight.snapshot import Snapshotter
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "app.py").write_text("x = 1\n")
    (repo / "test_app.py").write_text("def test_app():\n    assert True\n")
    cfg = HindsightConfig(project_dir=repo, hindsight_home=tmp_path / "hs")
    cfg.experiments.workers_max = 3
    store = Store(cfg.db_path)
    snap = Snapshotter(repo, cfg.snapshot_repo, DEFAULT_SNAPSHOT_EXCLUDES, 5.0)
    snap.ensure()
    commit = snap.snapshot("s1", 1, "post").commit
    turn_id = store.start_turn("s1", 1, "m1", commit)
    store.complete_turn(turn_id, "ok", commit, snapshot_files=1)
    store.mark_turn_usable(turn_id, 1, "test fixture")
    yield cfg, store, snap, commit
    store.close()

# ---------------------------------------------------------------- the parser

GIT_TESTS_ONLY = """diff --git a/hs_edge/test_c1.py b/hs_edge/test_c1.py
new file mode 100644
index 0000000..8302f2a
--- /dev/null
+++ b/hs_edge/test_c1.py
@@ -0,0 +1,3 @@
+def test_c1():
+    assert False
"""

GIT_PRODUCT = """diff --git a/scheduler.py b/scheduler.py
index 8302f2a..5a5bbf6 100644
--- a/scheduler.py
+++ b/scheduler.py
@@ -1,2 +1,2 @@
-x = 1
+x = 2
diff --git a/hs_edge/test_c1.py b/hs_edge/test_c1.py
new file mode 100644
--- /dev/null
+++ b/hs_edge/test_c1.py
@@ -0,0 +1,3 @@
+def test_c1():
+    assert False
"""

UNIFIED_TESTS_ONLY = """--- a/test_app.py
+++ b/test_app.py
@@ -1 +1 @@
-assert True
+assert False
"""

UNIFIED_PRODUCT = """--- a/app.py
+++ b/app.py
@@ -1 +1 @@
-x = 1
+x = 2
"""


def test_touches_product_code_rejects_hollow_git_patch():
    # Exactly the 28/28 signature from the night: only the harness's own
    # untracked edge tests, swept in by `git add -A`.
    assert _touches_product_code(GIT_TESTS_ONLY) is False


def test_touches_product_code_accepts_product_patch_git_format():
    # A real redesign rides along with the swept-up edge tests.
    assert _touches_product_code(GIT_PRODUCT) is True


def test_touches_product_code_unified_format():
    assert _touches_product_code(UNIFIED_PRODUCT) is True
    assert _touches_product_code(UNIFIED_TESTS_ONLY) is False


def test_touches_product_code_test_naming_conventions():
    assert _touches_product_code(
        "diff --git a/foo_test.py b/foo_test.py\n") is False
    assert _touches_product_code(
        "diff --git a/pkg/app.py b/pkg/app.py\n@@ -1 +1 @@\n-a = 1\n+a = 2\n") is True


def test_touches_product_code_empty_patch_is_not_product():
    assert _touches_product_code("") is False
    assert _touches_product_code("not a diff at all") is False


# ------------------------------------------------------------- the collector

class CollectSail:
    """UNIT TEST MOCK: a finished worker box with scripted result files."""

    def __init__(self, result: dict, patch: str = ""):
        self.result = result
        self.patch = patch
        self.terminated: list[str] = []
        self.read_paths: list[str] = []

    def read_file(self, box_id, path):
        self.read_paths.append(path)
        if path == "/work/result.json":
            return json.dumps(self.result).encode()
        if path == "/work/patch.diff":
            return self.patch.encode()
        raise SailClientError(f"no such file: {path}")

    def checkpoint(self, box_id, ttl_seconds=None, name=None):
        return "cp-x"

    def terminate(self, box_id):
        self.terminated.append(box_id)

    def sleep(self, box_id):
        pass


DONE_RESULT = {
    "exit_reason": "done",
    "summary": {"summary": "made the count shared", "addresses": ["R1"]},
    "diff_stat": {"files": 1, "added": 10, "removed": 2},
}


@pytest.fixture()
def exec_parts(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "app.py").write_text("x = 1\n")
    cfg = HindsightConfig(project_dir=repo, hindsight_home=tmp_path / "hs")
    store = Store(cfg.db_path)
    return cfg, store


def _seed_worker(store) -> str:
    ex_id = store.create_exploration(
        "s1", "clean_rewrite", "opencode", "t1", 1, {"target_ids": []})
    store.transition_exploration(ex_id, "PROVISIONING", "seed")
    store.transition_exploration(ex_id, "RUNNING", "seed")
    return ex_id


def _proposals(store) -> list:
    return store._query("SELECT id, exploration_id FROM proposals")


def test_collect_refuses_errored_worker(exec_parts):
    """The exact dead-workforce signature: driver wrote result.json with
    exit_reason=error and an (empty) patch — no proposal may exist."""
    cfg, store = exec_parts
    ex_id = _seed_worker(store)
    sail = CollectSail({"exit_reason": "error"}, patch=GIT_TESTS_ONLY)
    ex = Executor(cfg, store, None, sail, None)

    ex._collect(ex_id, "box-1")

    row = store.get_exploration(ex_id)
    assert row["state"] == "FAILED"
    assert row["exit_reason"].startswith("refused: error")
    assert _proposals(store) == []
    assert sail.terminated == ["box-1"]


def test_collect_keeps_the_code_of_a_worker_out_of_time_and_says_so(exec_parts):
    """Its code is real, even with no description of it. Critics and the agent are told."""
    cfg, store = exec_parts
    ex_id = _seed_worker(store)
    sail = CollectSail({"exit_reason": "budget_time"}, patch=UNIFIED_PRODUCT)
    ex = Executor(cfg, store, None, sail, None)

    ex._collect(ex_id, "box-1")

    row = store.get_exploration(ex_id)
    assert row["state"] == "GATING"
    assert row["exit_reason"] == "budget_time"
    proposals = _proposals(store)
    assert len(proposals) == 1
    assert "ran out of time" in str(store.get_proposal(proposals[0]["id"])["summary"])


def test_collect_still_refuses_a_worker_stopped_for_no_progress(exec_parts):
    cfg, store = exec_parts
    ex_id = _seed_worker(store)
    sail = CollectSail({"exit_reason": "no_progress"}, patch=UNIFIED_PRODUCT)
    Executor(cfg, store, None, sail, None)._collect(ex_id, "box-1")
    assert store.get_exploration(ex_id)["state"] == "FAILED"


def test_collect_accepts_finished_worker(exec_parts):
    """A done worker with a valid summary still becomes a proposal."""
    cfg, store = exec_parts
    ex_id = _seed_worker(store)
    sail = CollectSail(DONE_RESULT, patch=UNIFIED_PRODUCT)
    ex = Executor(cfg, store, None, sail, None)

    ex._collect(ex_id, "box-1")

    row = store.get_exploration(ex_id)
    assert row["state"] == "GATING"  # integrity passed; selection decides
    assert row["exit_reason"] == "done"
    props = _proposals(store)
    assert len(props) == 1 and props[0]["exploration_id"] == ex_id


# ------------------------------------------------------- the selection gate

def test_tests_only_patch_never_qualifies(parts):
    """Even with a perfect test result, a patch touching no product code is
    never a winner."""
    cfg, store, snap, commit = parts
    cfg.experiments.workers_max = 1  # escalation impossible -> terminal state
    sail = SelectionSail()
    run_id = seed_run(store, n_proposals=1)
    for p in store.run_proposals(run_id):
        store._exec("UPDATE proposals SET patch_blob=? WHERE id=?",
                    (store._blob_set(GIT_TESTS_ONLY), p["id"]))
    sel = make_selector(cfg, store, snap, sail)

    sel.on_exploration_finished(run_id)

    run = store.get_run(run_id)
    assert run["state"] == "NONE_QUALIFIED"
    reasons = {r["reason"] for r in store._query(
        "SELECT reason FROM gate_results WHERE stage='2_audit_gate'")}
    assert "worker found no product code to change" in reasons


def test_compute_patch_excludes_bytecode(tmp_path, monkeypatch):
    """Patch hygiene: `git add -A` used to sweep untracked
    __pycache__/*.pyc into every worker patch (seen live in the worker
    box verification: the debug patch was a .pyc binary diff) — the same
    sweep that laundered the dead workforce's empty patches. Bytecode
    must never ride into a proposal's patch."""
    import subprocess
    from hindsight.box import driver as drv

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "app.py").write_text("x = 1\n")
    subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=t", "-c",
         "user.email=t@t", "commit", "-qm", "base"], check=True)
    base = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        capture_output=True, text=True).stdout.strip()
    monkeypatch.setattr(drv, "REPO", repo)

    # simulate the worker's real work + pytest's bytecode droppings
    (repo / "app.py").write_text("x = 2\n")
    pyc = repo / "__pycache__"
    pyc.mkdir()
    (pyc / "app.cpython-311.pyc").write_bytes(b"\x00garbage")

    patch, stat = drv.compute_patch(base)
    assert "app.py" in patch
    assert "__pycache__" not in patch
    assert stat["files"] == 1 and stat["added"] == 1


def test_a_killed_worker_is_billed_and_keeps_its_transcript(exec_parts):
    cfg, store = exec_parts
    ex_id = _seed_worker(store)
    sail = CollectSail({"exit_reason": "budget_time", "input_tokens": 1_000_000,
                        "output_tokens": 500_000}, patch=UNIFIED_PRODUCT)
    Executor(cfg, store, None, sail, None)._collect(ex_id, "box-1")
    row = store._query("SELECT model, input_tokens, output_tokens, usd FROM costs "
                       "WHERE ref=? AND kind='model'", (ex_id,))[0]
    assert row["model"] == cfg.models.worker
    assert (row["input_tokens"], row["output_tokens"]) == (1_000_000, 500_000)
    assert row["usd"] > 0
    assert sail.read_paths.count("/work/events.jsonl") == 1


def test_opencode_worker_logs_activity_and_counts_all_tokens(tmp_path, monkeypatch):
    from hindsight.box import driver as drv

    monkeypatch.setattr(drv, "EVENTS_PATH", tmp_path / "events.jsonl")
    monkeypatch.setattr(drv, "STATUS_PATH", tmp_path / "status.json")
    monkeypatch.setattr(drv, "REPO", tmp_path)
    monkeypatch.setattr(drv.Path, "home", lambda: tmp_path)
    monkeypatch.setitem(drv._status, "input_tokens", 0)
    monkeypatch.setitem(drv._status, "output_tokens", 0)
    lines = [
        json.dumps({"type": "tool_use", "part": {"tool": "bash", "output": "x" * 9000}}),
        json.dumps({"part": {"type": "step-finish", "tokens": {
            "input": 10, "output": 5, "reasoning": 7, "cache": {"read": 100, "write": 3}}}}),
        json.dumps({"type": "text", "part": {"type": "text", "text": "done"}}),
    ]

    class Proc:
        stdout = iter(line + "\n" for line in lines)
        stderr = None

        def poll(self):
            return 0

        def wait(self):
            return 0

    monkeypatch.setattr(drv.subprocess, "Popen", lambda *a, **k: Proc())
    monkeypatch.setattr(drv.shutil, "which", lambda name: "/bin/" + name)
    monkeypatch.setenv("SAIL_API_KEY", "k")

    class Monitor:
        def bind(self, proc):
            pass

        def check(self):
            pass

    run = drv.harness_opencode({"prompt": "p", "model": "sail-research/m"}, Monitor())
    assert (run["input_tokens"], run["output_tokens"]) == (113, 12)
    assert (drv._status["input_tokens"], drv._status["output_tokens"]) == (113, 12)
    logged = [json.loads(x) for x in (tmp_path / "events.jsonl").read_text().splitlines()]
    agent = [e for e in logged if e["type"] == "agent"]
    assert len(agent) == 3 and len(agent[0]["raw"]) == drv._MAX_EVENT_CHARS


def test_a_compiled_program_stays_out_of_the_patch(tmp_path, monkeypatch):
    import subprocess

    from hindsight.box import driver as drv

    def git(*args):
        subprocess.run(["git", "-C", str(tmp_path), "-c", "user.name=a", "-c", "user.email=a@b",
                        *args], check=True, capture_output=True)

    git("init", "-q")
    (tmp_path / "app bin").write_bytes(b"\0\1\2old")
    (tmp_path / "main.go").write_text("package main\n")
    git("add", "-A")
    git("commit", "-qm", "base")
    monkeypatch.setattr(drv, "REPO", tmp_path)
    (tmp_path / "app bin").write_bytes(b"\0\1\2new")
    (tmp_path / "main.go").write_text("package main\n// changed\n")
    (tmp_path / "other.bin").write_bytes(b"\0\9")

    patch, stat = drv.compute_patch("HEAD")

    assert "main.go" in patch and "app bin" not in patch and "other.bin" not in patch
    assert "Binary files" not in patch and stat["files"] == 1
    clone = tmp_path / "c"
    subprocess.run(["git", "clone", "-q", str(tmp_path), str(clone)], check=True)
    subprocess.run(["git", "apply", "-"], cwd=clone, input=patch.encode(), check=True)
