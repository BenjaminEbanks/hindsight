"""Fork-at-nudge tests.

The fork machinery runs the REAL adoption engine on the forked store;
only the Sail surface and model calls are fakes (UNIT TEST MOCKS).
Scenario: the scripted main agent IGNORES the stage-2 design break (keeps
the in-process counter). The (faked) worker proposal carries a REAL patch
that fixes it. The fork must then show: ignore branch fails the hidden
tests, accept branch passes them — the nudge helped.
"""

from __future__ import annotations

import difflib
import json
from pathlib import Path

import pytest

from hindsight.eval.fork import run_forked_episode
from hindsight.eval.metrics import compute_metrics
from hindsight.eval.tasks import Task
from tests.unit.test_eval_episode import (
    fake_judge_fn,
    fake_model_fn,
)
from tests.unit.test_eval_tasks import COUNTER_FINAL, COUNTER_NAIVE
from tests.unit.test_orchestrator_flow import FakeSailClient


def make_patch(old: str, new: str, path: str) -> str:
    return "".join(difflib.unified_diff(
        old.splitlines(keepends=True), new.splitlines(keepends=True),
        fromfile=f"a/{path}", tofile=f"b/{path}"))


COUNTER_PATCH = make_patch(COUNTER_NAIVE, COUNTER_FINAL, "counter.py")


class PatchServingSail(FakeSailClient):
    """FakeSailClient whose workers produce a REAL, applicable patch."""

    def read_file(self, box_id, path):
        if path.endswith("patch.diff"):
            return COUNTER_PATCH.encode()
        return super().read_file(box_id, path)


def stubborn_agent_script():
    """A main agent that never revisits its stage-1 design."""
    return [
        {"reply": "built the counter with tests",
         "files": {"counter.py": COUNTER_NAIVE,
                   "test_counter.py": "from counter import Counter\n"
                                      "\n"
                                      "def test_counts():\n"
                                      "    c = Counter()\n"
                                      "    c.increment()\n"
                                      "    assert c.get_value() == 1\n"}},
        {"reply": "kept it simple, still in-process for now", "files": {}},
        {"reply": "no changes needed", "files": {}},
    ]


def scripted_factory(repo_dir, script):
    from hindsight.eval.harness import ScriptedAgent
    return ScriptedAgent(repo_dir, script)


FAST = {"settle_seconds": 0.05, "quiet_poll_seconds": 0.02,
        "worker_poll_seconds": 0.02, "quiet_timeout": 60}


def test_fork_measures_the_nudge_helping(tmp_path):
    task = Task.load("eval/tasks/counters.json")
    repo = task.materialize(tmp_path / "repo")
    treatment, fork, note = run_forked_episode(
        task, repo, agent_factory=scripted_factory,
        agent_script=stubborn_agent_script(), root_dir=tmp_path / "root",
        sail=PatchServingSail(), model_fn=fake_model_fn,
        judge_model_fn=fake_judge_fn,
        settings_overrides=dict(FAST))

    assert note == ""
    assert treatment.nudge_count >= 1
    assert fork is not None
    # The nudge arrived while stages remained: at stage 2's quiet wait.
    assert fork.nudge_stage == 1
    # The fork snapshot exists (repo + store + fork.json).
    fdir = tmp_path / "root" / "fork"
    assert (fdir / "fork.json").exists()
    assert (fdir / "store.sqlite3").exists()
    # The real adoption engine applied the winning proposal.
    assert fork.adopted is True, fork.adopt_error
    assert fork.adopt_error == ""
    # The measurement: ignoring the nudge leaves the broken design;
    # accepting it fixes the hidden tests. The nudge HELPED.
    assert fork.ignore.final.ran and fork.ignore.final.failed > 0
    assert fork.accept.final.ran and fork.accept.final.failed == 0
    assert fork.helped is True
    assert fork.delta_passed == fork.accept.final.passed - fork.ignore.final.passed


def test_fork_absent_when_no_nudge(tmp_path):
    """No-change task: no nudge -> no fork, and the note says so honestly."""
    from tests.unit.test_eval_episode import NothingToChangeSail, \
        todos_agent_script, fake_judge_fn, fake_model_fn

    task = Task.load("eval/tasks/todos.json")
    repo = task.materialize(tmp_path / "repo")
    treatment, fork, note = run_forked_episode(
        task, repo, agent_factory=scripted_factory,
        agent_script=todos_agent_script(), root_dir=tmp_path / "root",
        sail=NothingToChangeSail(), model_fn=fake_model_fn,
        judge_model_fn=fake_judge_fn,
        settings_overrides=dict(FAST))

    assert fork is None
    assert "no nudge" in note
    assert treatment.nudge_count == 0


# --------------------------------------------------------------- metrics

def test_compute_metrics_aggregates(tmp_path):
    from hindsight.eval.episode import EpisodeResult
    from hindsight.eval.fork import ForkOutcome
    from hindsight.eval.scoring import HiddenTestResult

    tasks = [Task.load("eval/tasks/counters.json"),
             Task.load("eval/tasks/todos.json")]

    def res(arm, task_id, ok, nudges=0, runs=None, spend=0.0,
            turns=3):
        return EpisodeResult(
            task_id=task_id, arm=arm, turns=turns, nudges=[
                {"proposal_id": "p", "text": "t", "shown_at": 1.0}
            ] * nudges, runs=runs or [], spend_usd=spend,
            final=HiddenTestResult(passed=3 if ok else 1,
                                   failed=0 if ok else 2, ran=True))

    treatment_counters = res(
        "treatment", "counters", ok=True, nudges=1, spend=1.5,
        runs=[{"id": "r1", "state": "DELIVERED", "exit_reason": "",
               "winner": "p1"},
              {"id": "r2", "state": "DROPPED_STALE", "exit_reason": "",
               "winner": None}])
    treatment_todos = res("treatment", "todos", ok=True, nudges=0)
    baseline = res("baseline_alone", "counters", ok=False)

    def fork_outcome(helped, delta):
        return ForkOutcome(
            task_id="counters", nudge_stage=1, proposal_id="p1",
            nudge_text="t", adopted=True, adopt_test_passed=None,
            accept=res("fork_accept", "counters", ok=helped),
            ignore=res("fork_ignore", "counters", ok=not helped))

    m = compute_metrics(tasks, [treatment_counters, treatment_todos,
                                baseline],
                        [fork_outcome(True, 2), fork_outcome(False, 0)])
    assert m.forks == 2 and m.helped == 1
    assert m.helped_rate == 0.5
    assert m.staleness_rate == 0.5       # 1 stale of 2 completed runs
    assert m.nudges_total == 1
    assert m.no_change_tasks == 1 and m.no_change_tasks_clean == 1
    assert m.arm_pass_rate("treatment") == 1.0
    assert m.arm_pass_rate("baseline_alone") == 0.0
    assert m.cost_per_useful_nudge == 1.5
    d = m.to_dict()
    assert d["no_change_control"] == "1/1 zero-nudge"
