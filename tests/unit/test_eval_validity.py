"""Pins the eval-validity fixes of each of these defects produced
false conclusions or hid evidence while the unit suite stayed green."""

from __future__ import annotations

import importlib.util
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from hindsight.eval.episode import EpisodeResult
from hindsight.eval.fork import ForkOutcome
from hindsight.eval.metrics import compute_metrics
from hindsight.eval.provenance import (
    build_provenance, comparable, content_hash)
from hindsight.eval.scoring import HiddenTestResult, run_hidden_tests
from hindsight.eval.tasks import Task

ROOT = Path(__file__).resolve().parents[2]


def load_script(name: str):
    spec = importlib.util.spec_from_file_location(
        f"script_{name}", ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


audit = load_script("audit_contracts")
analyze = load_script("analyze_swarm")
live_eval = load_script("live_eval")


# ------------------------------------------------------------ contract audit

def test_v2_specs_state_their_contracts_in_agent_visible_text():
    for path in sorted((ROOT / "eval/tasks/v2").glob("*.json")):
        assert audit.audit_task(str(path)) == []


def test_audit_ignores_requirements_the_agent_never_sees(tmp_path):
    """Requirements are ledger ground truth, never sent to the agent."""
    test = ("from m import C\n\ndef test_x():\n"
            "    assert C().items() == [('a', 1)]\n")
    task = {"id": "t", "stages": [{
        "message": "m.py: a C class with items().",
        "requirements": [{"id": "R1", "text": "items() returns (name, n) pairs"}],
        "hidden_tests": {"test_x.py": test}}]}
    path = tmp_path / "t.json"
    path.write_text(json.dumps(task))
    assert any("return shape" in p for p in audit.audit_task(str(path)))


# ------------------------------------------------------------ scoring

def scoring_task(tests: dict[str, str]) -> Task:
    return Task(id="t", title="t", stages=[{
        "message": "m", "hidden_tests": tests}],
        hidden_test_command=f"{sys.executable} -m pytest -q {{hidden_dir}}")


def test_collection_error_is_visible_and_expected_count_is_real(tmp_path):
    tests = {"test_a.py": "import missing_module\n\ndef test_1(): pass\n"
                          "def test_2(): pass\n"}
    task = scoring_task(tests)
    repo = task.materialize(tmp_path / "repo")
    result = run_hidden_tests(task, repo)
    assert result.expected_tests == 2
    assert result.passed == 0 and result.failed == 1
    assert result.collection_errors == 1
    assert not result.ok


def test_per_test_outcomes_are_recorded(tmp_path):
    tests = {"test_a.py": "def test_good(): pass\n"
                          "def test_bad():\n    assert 1 == 2\n"}
    task = scoring_task(tests)
    repo = task.materialize(tmp_path / "repo")
    result = run_hidden_tests(task, repo)
    assert {t["name"]: t["outcome"] for t in result.tests} == {
        "test_good": "passed", "test_bad": "failed"}


# ------------------------------------------------------------ metrics / fork

def episode(arm: str, passed: int = 0, failed: int = 0, **kw) -> EpisodeResult:
    return EpisodeResult(
        task_id="cart", arm=arm, turns=3,
        final=HiddenTestResult(passed, failed), **kw)


def test_helped_needs_a_real_pass_not_just_zero_failures():
    empty = ForkOutcome("t", 1, "p", "n", accept=episode("a"),
                        ignore=episode("i", failed=1))
    assert empty.helped is False
    real = ForkOutcome("t", 1, "p", "n", accept=episode("a", passed=3),
                       ignore=episode("i", passed=1, failed=2))
    assert real.helped is True and real.delta_passed == 2


@pytest.mark.parametrize("arm", ["treatment", "treatment_relay",
                                 "treatment_diff"])
def test_metrics_count_every_pipeline_arm(arm):
    task = Task(id="cart", title="c", no_change=True,
                stages=[{"message": "m"}])
    result = episode(arm, passed=1, runs=[{"state": "DROPPED_STALE"}])
    m = compute_metrics([task], [result], [])
    assert m.completed_runs == 1 and m.no_change_tasks == 1


# ------------------------------------------------------------ worker health

def store_with(tmp_path: Path, rows: list[tuple[str, str]]) -> Path:
    db = tmp_path / "hs" / "events.sqlite3"
    db.parent.mkdir()
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE explorations (state TEXT, exit_reason TEXT)")
    con.executemany("INSERT INTO explorations VALUES (?, ?)", rows)
    con.commit()
    con.close()
    return tmp_path


def test_worker_health_counts_refused_workers(tmp_path):
    root = store_with(tmp_path, [("FAILED", "refused: error")] * 10
                      + [("SHOWN", "done")] * 2)
    health = live_eval._worker_health(root)
    assert health["explorations_errored"] == 10
    assert health["explorations_succeeded"] == 2
    assert health["workers_degraded"] is True
    assert health["workers_broken"] is False


def test_worker_health_flags_errored_workers_in_productive_states(tmp_path):
    """The dead-workforce shape: states march on, exit reasons say error."""
    root = store_with(tmp_path, [("SHOWN", "error")] * 7)
    assert live_eval._worker_health(root)["workers_broken"] is True


# ------------------------------------------------------------ provenance

def shipped_tree(tmp_path: Path) -> Path:
    for rel in ("hindsight/a.py", "eval/tasks/t.json",
                "scripts/s.py", "pyproject.toml", "README.md"):
        f = tmp_path / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(rel)
    return tmp_path


def test_content_hash_tracks_tasks_but_not_bytecode(tmp_path):
    tree = shipped_tree(tmp_path)
    before = content_hash(tree)
    cache = tree / "hindsight/__pycache__"
    cache.mkdir()
    (cache / "a.pyc").write_bytes(b"x")
    assert content_hash(tree) == before
    (tree / "eval/tasks/t.json").write_text("changed")
    assert content_hash(tree) != before


def test_results_without_provenance_are_never_comparable(tmp_path):
    prov = build_provenance(shipped_tree(tmp_path), model="m")
    assert comparable(prov, dict(prov))
    assert not comparable(None, prov)
    assert not comparable(prov, {**prov, "worker_model": "other"})


def test_swarm_refuses_to_resume_into_another_vintage(tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    (out / "cart__baseline_alone__r0.json").write_text(json.dumps({"final": {}}))
    r = subprocess.run(
        [sys.executable, "scripts/live_eval_swarm.py",
         "--tasks", "eval/tasks/v2/cart.json", "--arms", "baseline_alone",
         "--tag", "t", "--out", str(out), "--project", ".", "--real-key-in-box"],
        cwd=ROOT, capture_output=True, text=True)
    assert r.returncode == 2, r.stderr
    assert "REFUSING" in r.stdout


def test_swarm_wont_put_the_real_key_in_a_box_unless_told_to(tmp_path):
    r = subprocess.run(
        [sys.executable, "scripts/live_eval_swarm.py",
         "--tasks", "eval/tasks/v2/cart.json", "--arms", "baseline_alone",
         "--tag", "t", "--out", str(tmp_path), "--project", "."],
        cwd=ROOT, capture_output=True, text=True)
    assert r.returncode == 2
    assert "real key" in r.stderr


# ------------------------------------------------------------ analysis

def write_episode(out: Path, task: str, arm: str, rep: int, ok: bool) -> None:
    (out / f"{task}__{arm}__r{rep}.json").write_text(json.dumps({
        "final": {"ran": True, "ok": ok, "passed": 3 if ok else 1,
                  "failed": 0 if ok else 2}}))


def calibration_output(tmp_path, capsys, monkeypatch, streamed, ceiling):
    for rep in range(10):
        write_episode(tmp_path, "t", "baseline_alone", rep, rep < streamed)
        write_episode(tmp_path, "t", "full_context", rep, rep < ceiling)
    monkeypatch.setattr(sys, "argv", ["x", str(tmp_path), "--calibrate"])
    analyze.main()
    return capsys.readouterr().out


def test_calibration_keeps_a_task_with_room(tmp_path, capsys, monkeypatch):
    out = calibration_output(tmp_path, capsys, monkeypatch, 3, 9)
    assert "KEEP" in out and "can show an effect: ['t']" in out


def test_calibration_drops_a_task_that_already_passes(tmp_path, capsys,
                                                      monkeypatch):
    out = calibration_output(tmp_path, capsys, monkeypatch, 9, 10)
    assert "no room to improve" in out and "NONE" in out


def test_calibration_flags_a_task_failing_with_full_context(tmp_path, capsys,
                                                            monkeypatch):
    out = calibration_output(tmp_path, capsys, monkeypatch, 0, 0)
    assert "fails even with full context" in out


def test_analysis_reads_the_relay_and_diff_arms(tmp_path, capsys, monkeypatch):
    write_episode(tmp_path, "t", "baseline_alone", 0, False)
    write_episode(tmp_path, "t", "treatment_diff", 0, True)
    monkeypatch.setattr(sys, "argv", ["x", str(tmp_path)])
    analyze.main()
    out = capsys.readouterr().out
    assert "treatment_diff" in out and "1/1 vs 0/1" in out


def test_fisher_matches_known_values():
    assert analyze.fisher(0, 2, 2, 0) == pytest.approx(1 / 3)
    assert analyze.fisher(0, 4, 4, 0) == pytest.approx(0.0286, abs=1e-3)
