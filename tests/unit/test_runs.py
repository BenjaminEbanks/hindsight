"""Run manager tests. All Sail/model dependencies are injected fakes (UNIT TEST
MOCKS)."""

from __future__ import annotations

import json

import pytest

from hindsight.budget import BudgetManager
from hindsight.config import HindsightConfig
from hindsight.runs import OPEN_WORKER, RunManager
from hindsight.store import InvalidTransition, Store


class FakeExecutor:
    def __init__(self):
        self.submitted: list[tuple] = []

    def submit(self, plan, session_id, base_commit, harness="pi", *,
               samples=None, run_id=None, **kw):
        self.submitted.append((plan, samples, run_id, kw))
        return [f"ex-{len(self.submitted)}"]


@pytest.fixture()
def env(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "app.py").write_text("x = 1\n")
    cfg = HindsightConfig(project_dir=repo, hindsight_home=tmp_path / "hs")
    store = Store(cfg.db_path)
    budget = BudgetManager(store, cfg)
    yield cfg, store, budget
    store.close()


def test_run_launches_the_fixed_roster(env):
    cfg, store, budget = env
    executor = FakeExecutor()
    mgr = RunManager(cfg, store, budget, executor)
    run_id = mgr.start_audit_run("s1", ["R1"], "basecommit", "t1", 3,
                                 directions=["a: split it", "b: one model"])

    run = store.get_run(run_id)
    assert run["state"] == "LAUNCHED"
    assert run["difficulty"] == "directions"
    assert run["difficulty_source"] == "auditor"
    assert run["n_workers"] == 3
    assert json.loads(run["target_requirement_ids_json"]) == ["R1"]
    assert [s[0].strategy for s in executor.submitted] == [
        "direction_1", "direction_2", OPEN_WORKER]
    assert all(s[1] == 1 for s in executor.submitted)
    assert all(s[2] == run_id for s in executor.submitted)
    assert all(s[0].base_turn_n == 3 for s in executor.submitted)


def test_budget_refusal_is_a_recorded_state(env):
    cfg, store, budget = env
    cfg.budget.per_session_usd = 0.01
    executor = FakeExecutor()
    mgr = RunManager(cfg, store, budget, executor)
    run_id = mgr.start_audit_run("s1", ["R1"], "basecommit", "t1", 3,
                                 session_start=1e9)
    run = store.get_run(run_id)
    assert run["state"] == "BUDGET_REFUSED"
    assert executor.submitted == []
    assert run["exit_reason"]


def test_workers_stop_at_workers_max(env):
    cfg, store, budget = env
    cfg.experiments.workers_max = 3
    executor = FakeExecutor()
    mgr = RunManager(cfg, store, budget, executor)
    run_id = mgr.start_audit_run("s1", ["R1"], "basecommit", "t1", 3,
                                 directions=["a", "b", "c", "d"])
    assert store.get_run(run_id)["n_workers"] == 3
    assert [s[0].strategy for s in executor.submitted] == [
        "direction_1", "direction_2", OPEN_WORKER]


def test_strategies_narrow_the_roster(env):
    cfg, store, budget = env
    executor = FakeExecutor()
    mgr = RunManager(cfg, store, budget, executor)
    mgr.start_audit_run("s1", ["R1"], "basecommit", "t1", 3,
                        strategies=[OPEN_WORKER], directions=["a"])
    assert [s[0].strategy for s in executor.submitted] == [OPEN_WORKER]


def test_no_changed_requirement_no_run(env):
    cfg, store, budget = env
    mgr = RunManager(cfg, store, budget, FakeExecutor())
    assert mgr.start_audit_run("s1", [], "basecommit", "t1", 3) is None
    assert store._query("SELECT * FROM runs") == []


def test_final_worker_gets_the_plan_and_the_mark(env):
    cfg, store, budget = env
    executor = FakeExecutor()
    mgr = RunManager(cfg, store, budget, executor)
    run_id = store.create_run("s1", 1, ["R1"])
    ex_id = mgr.launch_synthesis(store.get_run(run_id), "c", "the plan")
    assert ex_id == "ex-1"
    plan, _, _, kw = executor.submitted[0]
    assert plan.strategy == "council_synthesis"
    assert kw["mark_synthesis"] is True
    assert kw["extra_prompt"] == {"directive": "the plan"}


def test_run_state_machine_rejects_illegal_transition(env):
    cfg, store, budget = env
    run_id = store.create_run("s1", 1, [])
    with pytest.raises(InvalidTransition):
        store.transition_run(run_id, "DELIVERED", "skip ahead")
    store.transition_run(run_id, "PLANNED", "ok")
    store.transition_run(run_id, "BUDGET_REFUSED", "refused")
    with pytest.raises(InvalidTransition):
        store.transition_run(run_id, "LAUNCHED", "dead runs don't launch")
    row = store.get_run(run_id)
    assert row["state"] == "BUDGET_REFUSED"
    assert row["finished_at"] is not None


def test_explorations_link_to_run(env):
    cfg, store, budget = env
    run_id = store.create_run("s1", 1, [])
    ex_id = store.create_exploration("s1", "clean_rewrite", "pi", "t1", 1,
                                      {"target_ids": []}, run_id=run_id)
    assert store.run_explorations(run_id)[0]["id"] == ex_id


def test_final_worker_is_recorded_on_the_turn_its_code_came_from(tmp_path):
    from hindsight.store import Store

    store = Store(tmp_path / "e.sqlite3")
    done = store.start_turn("s1", 1, "m1", "pre")
    store.complete_turn(done, "ok", "commit-1", snapshot_files=1)
    store.start_turn("s1", 2, "m2", "pre2")  # still in progress
    assert store.turn_with_snapshot("s1", "commit-1")["n"] == 1
    assert store.latest_turn("s1")["n"] == 2
    store.close()


def _run_with(directions, workers_max=8):
    from types import SimpleNamespace
    cfg = SimpleNamespace(experiments=SimpleNamespace(workers_max=workers_max))
    return RunManager(cfg, None, None, None)._worker_plans(["R"], "t", 1, directions)


def test_no_directions_still_runs_the_open_worker():
    assert [(p.strategy, extra) for p, extra in _run_with([])] == [(OPEN_WORKER, {})]


def test_each_worker_gets_its_own_direction():
    plans = _run_with(["a", "b"])
    assert [extra for _, extra in plans] == [{"direction": "a"}, {"direction": "b"}, {}]


def test_the_open_worker_survives_the_cap():
    assert [p.strategy for p, _ in _run_with(["a", "b", "c"], workers_max=2)] == [
        "direction_1", OPEN_WORKER]
