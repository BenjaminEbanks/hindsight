"""The audit: detection, integrity with quoted rewrites, worker package, gate,
and the evidence the nudge states. Sail, model and
executor dependencies are injected fakes (UNIT TEST MOCKS). Nothing here makes
a live call or spends money."""

from __future__ import annotations

import json

import pytest

from hindsight.config import DEFAULT_SNAPSHOT_EXCLUDES, HindsightConfig
from hindsight.council import Council
from hindsight.delivery import DeliveryManager
from hindsight.eval.episode import EvalEpisode
from hindsight.eval.harness import ScriptedAgent
from hindsight.executor import Executor, STRATEGY_INSTRUCTIONS
from hindsight.gate.integrity import check_integrity
from hindsight.planner import PlannedExploration
from hindsight.requirement_audit import (check_audit_integrity,
                                         edited_assert_findings)
from hindsight.runs import RunManager
from hindsight.budget import BudgetManager
from hindsight.session_gate import evaluate_gate
from hindsight.snapshot import Snapshotter
from hindsight.store import Store, built_on_key
from hindsight.delivery import DELIVERY_MARKER
from hindsight.design_review import run_reason_key
from tests.unit.test_eval_episode import (FAST, ReviewingAgent, counter_agent_script,
                                          counters_repo, fake_judge_fn, fake_model_fn)
from tests.unit.test_orchestrator_flow import FakeSailClient
from tests.unit.test_selection import (FakeEnv, FakeExecutor, SelectionSail,
                                       junit, make_selector, parts)

USER_MESSAGES = [
    "Build me a counter module.",
    "Actually the count must start at ten, not zero.",
]
QUOTE = "must start at ten"

REWRITE_PATCH = (
    "diff --git a/test_app.py b/test_app.py\n"
    "--- a/test_app.py\n"
    "+++ b/test_app.py\n"
    "@@ -1,2 +1,2 @@\n"
    "-def test_start():\n"
    "-    assert start() == 0\n"
    "+def test_start():\n"
    "+    assert start() == 10\n")
BODY_EDIT_PATCH = (
    "diff --git a/test_app.py b/test_app.py\n"
    "--- a/test_app.py\n"
    "+++ b/test_app.py\n"
    "@@ -1,2 +1,2 @@ def test_start():\n"
    "-    assert start() == 0\n"
    "+    assert start() >= 0\n")
PRODUCT_PATCH = (
    "diff --git a/app.py b/app.py\n"
    "--- a/app.py\n"
    "+++ b/app.py\n"
    "@@ -1 +1 @@\n"
    "-x = 1\n"
    "+x = 2\n")


def listed(test="test_start", reason="the user changed the start value",
           quote=QUOTE) -> dict:
    return {"tests_changed": [
        {"test": test, "reason": reason, "user_quote": quote}]}


# ---------------------------------------------------------------- integrity

def test_global_integrity_gate_is_not_loosened():
    assert not check_integrity(REWRITE_PATCH).passed


def test_rewrite_excused_by_verbatim_user_quote_and_recorded():
    result = check_audit_integrity(REWRITE_PATCH, listed(), USER_MESSAGES)
    assert result.passed
    assert result.findings == []
    assert {c["test"] for c in result.allowed} == {"test_start"}
    assert all(c["user_message"] == 2 and c["user_quote"] == QUOTE
               for c in result.allowed)


def test_rewrite_for_the_old_structure_needs_a_reason_but_no_quote():
    summary = listed(quote="", reason="it only checked the old internal dict")
    result = check_audit_integrity(REWRITE_PATCH, summary, USER_MESSAGES)
    assert result.passed
    assert result.allowed[0]["reason"] == "it only checked the old internal dict"


def test_quote_matching_ignores_case_and_line_wrapping():
    summary = listed(quote="MUST start\n  at ten")
    assert check_audit_integrity(REWRITE_PATCH, summary, USER_MESSAGES).passed


@pytest.mark.parametrize("summary, why", [
    ({"tests_changed": []}, "not listed"),
    ({}, "not listed"),
    ("(no summary)", "not listed"),
    ({"tests_changed": ["test_start", None, 7]}, "not listed"),
    (listed(test="test_other"), "not listed"),
    (listed(reason=""), "no reason"),
    (listed(quote="the requirement says ten"), "not verbatim"),
    # Real words from a message, too short to tie to anything.
    (listed(quote="start at"), "not verbatim"),
])
def test_rewrite_rejected_unlisted_or_with_a_false_quote(summary, why):
    result = check_audit_integrity(REWRITE_PATCH, summary, USER_MESSAGES)
    assert not result.passed
    assert result.allowed == []
    assert result.denied and why in result.denied[0]["why_denied"]


@pytest.mark.parametrize("line", [
    "+@pytest.mark.skip", "+@pytest.mark.xfail", "+    assert True"])
def test_weakening_markers_are_never_excused(line):
    patch = REWRITE_PATCH + line + "\n"
    result = check_audit_integrity(patch, listed(), USER_MESSAGES)
    assert not result.passed
    assert result.denied == []


def test_assertion_edit_in_a_test_body_is_caught():
    """The regex gate cannot see this, and with no flip required it would let
    a worker pass the regression gate by editing the test."""
    assert check_integrity(BODY_EDIT_PATCH).passed
    assert not check_audit_integrity(
        BODY_EDIT_PATCH, {"tests_changed": []}, USER_MESSAGES).passed
    assert check_audit_integrity(BODY_EDIT_PATCH, listed(), USER_MESSAGES).passed


def test_unattributable_assertion_edit_cannot_be_excused():
    patch = ("diff --git a/test_app.py b/test_app.py\n"
             "@@ -1,2 +1,2 @@\n"
             " def helper():\n"
             "-    assert start() == 0\n"
             "+    assert start() >= 0\n")
    findings = edited_assert_findings(patch)
    assert [f["test"] for f in findings] == [None]
    assert not check_audit_integrity(patch, listed(), USER_MESSAGES).passed


def test_new_tests_and_product_assertions_are_not_flagged():
    new_file = ("diff --git a/test_new.py b/test_new.py\n"
                "new file mode 100644\n"
                "--- /dev/null\n"
                "+++ b/test_new.py\n"
                "@@ -0,0 +1,2 @@\n"
                "+def test_new():\n"
                "+    assert start() == 10\n")
    product = ("diff --git a/app.py b/app.py\n"
               "@@ -1 +1 @@\n"
               "-    assert x\n"
               "+    pass\n")
    assert edited_assert_findings(new_file) == []
    assert edited_assert_findings(product) == []


# ------------------------------------------------------------------ trigger

# ------------------------------------------------------------- run planning

class RecordingExecutor:
    def __init__(self):
        self.calls = []

    def submit(self, plan, session_id, base_commit, **kw):
        self.calls.append((plan, kw))
        return [f"ex-{len(self.calls)}"]


def _run_manager(tmp_path):
    cfg = HindsightConfig(project_dir=tmp_path, hindsight_home=tmp_path / "hs")
    store = Store(cfg.db_path)
    executor = RecordingExecutor()
    return RunManager(cfg, store, BudgetManager(store, cfg), executor), store, executor


def test_audit_run_launches_roster(tmp_path):
    manager, store, executor = _run_manager(tmp_path)

    run_id = manager.start_audit_run("s1", ["R2"], "abc", "t2", 2, directions=["a", "b"])

    run = store.get_run(run_id)
    assert run["state"] == "LAUNCHED"
    assert json.loads(run["target_requirement_ids_json"]) == ["R2"]
    assert len(executor.calls) == 3
    for plan, kw in executor.calls:
        assert plan.target_ids == ["R2"]
    store.close()


def test_audit_run_needs_a_changed_requirement(tmp_path):
    manager, store, executor = _run_manager(tmp_path)
    assert manager.start_audit_run("s1", [], "abc", "t2", 2) is None
    assert executor.calls == []
    store.close()


# ----------------------------------------------------------- worker package

class CollectSail(FakeSailClient):
    """UNIT TEST MOCK: a finished worker with a given patch and summary."""

    def __init__(self, patch: str, summary: dict):
        super().__init__()
        self.patch = patch
        self.summary = summary

    def read_file(self, box_id, path):
        if path.endswith("patch.diff"):
            return self.patch.encode()
        if path.endswith("result.json"):
            return json.dumps({"exit_reason": "done", "summary": self.summary,
                               "diff_stat": {"files": 1}}).encode()
        return super().read_file(box_id, path)


@pytest.fixture()
def audit_executor(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    cfg = HindsightConfig(project_dir=repo, hindsight_home=tmp_path / "hs")
    store = Store(cfg.db_path)
    tid = store.start_turn("s1", 1, "m1", "pre")
    store.add_messages("s1", tid, [
        {"id": f"m{i}", "role": "user", "text": t, "ts": float(i)}
        for i, t in enumerate(USER_MESSAGES)] + [
        {"id": "a", "role": "assistant", "text": "AGENT CLAIM", "ts": 5.0}])
    snap = Snapshotter(repo, cfg.snapshot_repo, DEFAULT_SNAPSHOT_EXCLUDES, 5.0)

    def seed():
        run_id = store.create_run("s1", 1, ["R2"])
        ex_id = store.create_exploration(
            "s1", "edge_first", "opencode", tid, 1, {}, run_id=run_id)
        store.transition_exploration(ex_id, "PROVISIONING", "seed")
        store.transition_exploration(ex_id, "RUNNING", "seed")
        return ex_id

    def make(sail=None):
        return Executor(cfg, store, snap, sail or FakeSailClient(), FakeEnv())

    yield make, seed, store
    store.close()


def _task_prompt(executor, ex_id, strategy="edge_first"):
    plan = PlannedExploration(strategy=strategy, base_turn_id="t", base_turn_n=2,
                              target_ids=["R2"])
    return executor._task_json(ex_id, plan, "opencode", "c",
                               session_id="s1")["prompt"]


def test_audit_worker_gets_verbatim_messages_in_order(audit_executor):
    make, seed, _ = audit_executor
    prompt = _task_prompt(make(), seed())
    first, second = (prompt.index(m) for m in USER_MESSAGES)
    assert first < second
    assert "/work/qa" not in prompt
    assert "If the user did not ask for it, do not require it" in prompt
    # The agent's own account of its work is not part of the specification.
    assert "AGENT CLAIM" not in prompt and "SESSION TRANSCRIPT" not in prompt
    assert "hs_edge" not in prompt


def test_user_text_with_braces_survives_verbatim(audit_executor):
    make, seed, store = audit_executor
    tid = store.start_turn("s1", 2, "m9", "pre")
    text = 'Return {"a": 1} and keep {test_command} literal.'
    store.add_messages("s1", tid, [
        {"id": "m9", "role": "user", "text": text, "ts": 9.0}])
    assert text in _task_prompt(make(), seed())


@pytest.mark.parametrize("strategy", sorted(STRATEGY_INSTRUCTIONS))
def test_personas_never_mention_edge_tests(strategy):
    text = STRATEGY_INSTRUCTIONS[strategy].lower()
    assert "edge test" not in text and "failing test" not in text


def test_final_worker_follows_the_plan_but_the_user_wins(audit_executor):
    make, seed, _ = audit_executor
    plan = PlannedExploration(strategy="council_synthesis", base_turn_id="t",
                              base_turn_n=2, target_ids=["R2"])
    prompt = make()._task_json(seed(), plan, "opencode", "c",
                               session_id="s1",
                               extra_prompt={"directive": "THE PLAN"})["prompt"]
    assert "THE PLAN" in prompt
    assert "the user's newest messages win" in prompt
    assert "/work/updates" in prompt
    assert "EXACTLY" not in prompt
    assert USER_MESSAGES[1] in prompt


def _integrity_row(store):
    row = store._query_one(
        "SELECT * FROM gate_results WHERE stage='0_integrity'")
    return row, json.loads(row["features_json"])


def test_collect_excuses_quoted_rewrite_and_records_it(audit_executor):
    make, seed, store = audit_executor
    ex_id = seed()
    sail = CollectSail(REWRITE_PATCH + PRODUCT_PATCH, listed())
    box = sail.create_box("hs-x")
    make(sail)._collect(ex_id, box.id)

    row, features = _integrity_row(store)
    assert row["passed"] == 1
    assert {c["test"] for c in features["allowed_test_changes"]} == {"test_start"}
    assert features["allowed_test_changes"][0]["user_quote"] == QUOTE
    assert store.get_exploration(ex_id)["state"] == "GATING"


def test_collect_rejects_unquoted_rewrite(audit_executor):
    make, seed, store = audit_executor
    ex_id = seed()
    sail = CollectSail(REWRITE_PATCH, {"tests_changed": []})
    box = sail.create_box("hs-x")
    make(sail)._collect(ex_id, box.id)

    row, features = _integrity_row(store)
    assert row["passed"] == 0
    assert features["denied_test_changes"]
    assert store.get_exploration(ex_id)["state"] == "DISCARDED"


# ---------------------------------------------------------------- selection

class AuditSail(SelectionSail):
    """UNIT TEST MOCK: project-suite results differ by which side the box is."""

    def __init__(self, current: dict, proposal: dict | None = None):
        super().__init__(current=current,
                         proposal=current if proposal is None else proposal)
        self.commands: list[str] = []

    def run(self, box_id, command, *, timeout=None, cwd=None, env=None):
        self.commands.append(" ".join(command))
        return super().run(box_id, command)


def seed_audit_run(store, *, n=1, patch=PRODUCT_PATCH, excused=(), final=True) -> str:
    """`final`: final-worker designs, already on their last try, so a test failure
    ends the run. The retry has its own test in test_council."""
    run_id = store.create_run("s1", 1, ["R1"])
    if final:
        store.kv_set(f"final_retried:{run_id}", "1")
    store.transition_run(run_id, "PLANNED", "seed")
    for i in range(n):
        ex_id = store.create_exploration(
            "s1", "clean_rewrite", "opencode", "t1", 1,
            {"target_ids": ["R1"], "synthesis": final}, run_id=run_id)
        latest = store.latest_usable_snapshot("s1")
        if latest is not None:
            store.kv_set(built_on_key(ex_id), latest["post_snapshot"])
        for state in ("PROVISIONING", "RUNNING", "TESTING", "GATING"):
            store.transition_exploration(ex_id, state, "seed")
        pid = store.save_proposal(ex_id, patch, {"summary": f"p{i}"},
                                  {"files": 1, "added": 1, "removed": 1})
        store.save_gate_result(pid, "0_integrity", True, {
            "findings": [],
            "allowed_test_changes": [{"test": t} for t in excused]}, "ok")
    store.transition_run(run_id, "LAUNCHED", "seed")
    return run_id


def _gate(store, run_id) -> dict:
    row = store._query_one(
        "SELECT g.* FROM gate_results g JOIN proposals p ON p.id=g.proposal_id "
        "JOIN explorations e ON e.id=p.exploration_id "
        "WHERE e.run_id=? AND g.stage='2_audit_gate'", (run_id,))
    return {"passed": row["passed"], "reason": row["reason"],
            **json.loads(row["features_json"])}


def test_qualifies_on_no_regressions(parts):
    cfg, store, snap, _ = parts
    sail = AuditSail(current={"test_app": "pass"},
                     proposal={"test_app": "pass", "test_new": "pass",
                               "test_new2": "fail"})
    run_id = seed_audit_run(store)
    sel = make_selector(cfg, store, snap, sail)
    sel.council = Council(cfg, store, lambda p: pytest.fail("lone design"))

    sel.on_exploration_finished(run_id)

    run = store.get_run(run_id)
    assert run["state"] == "READY_TO_DELIVER"
    p = store.get_proposal(run["winner_proposal_id"])
    assert p["qualified"] is True
    assert p["requirement_flips"] == []
    gate = _gate(store, run_id)
    assert gate["existing_tests_still_passing"] == 1
    # Recorded, and a passing worker test earned nothing.
    assert gate["worker_tests_unverified"] == {
        "passed": ["test_new"], "not_passed": ["test_new2"]}
    assert "nothing shows it is better" in gate["reason"]


def test_matrix_runs_only_the_project_suite(parts):
    cfg, store, snap, _ = parts
    sail = AuditSail(current={"test_app": "pass"})
    sel = make_selector(cfg, store, snap, sail)
    sel.on_exploration_finished(seed_audit_run(store))
    matrix = [c for c in sail.commands if "junitxml" in c]
    assert matrix and not any("hs_edge" in c or "hs_heldout" in c for c in matrix)


def test_regression_disqualifies(parts):
    cfg, store, snap, _ = parts
    sail = AuditSail(current={"test_app": "pass"}, proposal={"test_app": "fail"})
    run_id = seed_audit_run(store)
    make_selector(cfg, store, snap, sail).on_exploration_finished(run_id)
    gate = _gate(store, run_id)
    assert not gate["passed"] and gate["regressions"] == ["test_app"]


def test_vanished_test_is_a_regression_unless_excused(parts):
    cfg, store, snap, _ = parts
    current = {"test_app": "pass", "test_old": "pass"}
    proposal = {"test_app": "pass"}

    run_id = seed_audit_run(store)
    make_selector(cfg, store, snap, AuditSail(current, proposal)
                  ).on_exploration_finished(run_id)
    assert _gate(store, run_id)["regressions"] == ["test_old (vanished)"]

    excused_run = seed_audit_run(store, excused=["test_old"])
    make_selector(cfg, store, snap, AuditSail(current, proposal)
                  ).on_exploration_finished(excused_run)
    assert store.get_run(excused_run)["state"] == "READY_TO_DELIVER"
    assert _gate(store, excused_run)["tests_rewritten"] == [{"test": "test_old"}]


def test_excused_rewrite_that_fails_is_still_a_regression(parts):
    cfg, store, snap, _ = parts
    sail = AuditSail({"test_old": "pass"}, {"test_old": "fail"})
    run_id = seed_audit_run(store, excused=["test_old"])
    make_selector(cfg, store, snap, sail).on_exploration_finished(run_id)
    assert _gate(store, run_id)["regressions"] == ["test_old"]


def test_empty_matrix_is_no_evidence(parts):
    cfg, store, snap, _ = parts
    sail = AuditSail(current={"test_app": "pass"}, proposal={})
    run_id = seed_audit_run(store)
    make_selector(cfg, store, snap, sail).on_exploration_finished(run_id)
    gate = _gate(store, run_id)
    assert not gate["passed"] and gate["reason"] == "no executed test evidence"


def test_existing_failure_fixed_is_recorded_as_real_evidence(parts):
    cfg, store, snap, _ = parts
    sail = AuditSail({"test_app": "fail"}, {"test_app": "pass"})
    run_id = seed_audit_run(store)
    make_selector(cfg, store, snap, sail).on_exploration_finished(run_id)
    assert _gate(store, run_id)["existing_failures_fixed"] == ["test_app"]


def test_no_change_ends_the_run_without_paying_for_more_workers(parts):
    cfg, store, snap, _ = parts
    sail = AuditSail(current={"test_app": "pass"})
    executor = FakeExecutor()
    tests_only = ("diff --git a/test_new.py b/test_new.py\n"
                  "--- /dev/null\n+++ b/test_new.py\n"
                  "@@ -0,0 +1 @@\n+def test_new(): pass\n")
    run_id = seed_audit_run(store, n=2, patch=tests_only, final=False)
    make_selector(cfg, store, snap, sail, executor=executor
                  ).on_exploration_finished(run_id)
    run = store.get_run(run_id)
    assert run["state"] == "NONE_QUALIFIED"
    assert "no worker changed product code" in store._query_one(
        "SELECT reason FROM run_events WHERE run_id=? AND to_state='NONE_QUALIFIED'",
        (run_id,))["reason"]
    assert executor.submitted == [] and sail.apply_calls == 0


# ----------------------------------------------------------------- delivery

def _ready_run(store, gate: dict) -> tuple:
    tid = store.start_turn("s1", 2, "m2", "pre")
    run_id = store.create_run("s1", 1, ["T2"])
    store.kv_set(run_reason_key(run_id), "start at ten")
    ex_id = store.create_exploration("s1", "clean_rewrite", "opencode", tid, 1,
                                     {}, run_id=run_id)
    pid = store.save_proposal(ex_id, PRODUCT_PATCH, {"summary": "s"},
                              {"files": 1, "added": 3, "removed": 2})
    store.save_gate_result(pid, "2_audit_gate", True, gate, "ok")
    return store.get_run(run_id), store.get_proposal(pid)


GATE = {"existing_tests_still_passing": 4, "existing_failures_fixed": [],
        "worker_tests_unverified": {"passed": ["a", "b"], "not_passed": ["c"]},
        "tests_rewritten": []}


@pytest.mark.parametrize("model_text", ["", "An alternative version is ready."])
def test_nudge_states_what_evidence_exists(parts, model_text):
    cfg, store, _, _ = parts
    run, proposal = _ready_run(store, GATE)
    text = DeliveryManager(cfg, store, lambda p: model_text)._render_nudge(run, proposal)
    assert "No failing test prompted it" in text
    assert "4 existing test(s) still pass" in text
    assert "3 new test(s) (1 not passing) are its own and unverified" in text
    assert "previously-failing" not in text
    assert "conversation message #2" in text


def test_nudge_evidence_survives_a_long_model_answer(parts):
    cfg, store, _, _ = parts
    run, proposal = _ready_run(store, GATE)
    text = DeliveryManager(cfg, store, lambda p: "x" * 5000)._render_nudge(run, proposal)
    assert len(text) <= 2000 and text.endswith("are its own and unverified.")


def test_nudge_prompt_forbids_claiming_a_bug_and_carries_no_flips(parts):
    cfg, store, _, _ = parts
    seen = []
    run, proposal = _ready_run(store, GATE)
    DeliveryManager(cfg, store, lambda p: seen.append(p) or "ok")._render_nudge(
        run, proposal)
    assert "never say or imply that a bug was found" in seen[0]
    assert "start at ten" in seen[0]
    assert "now pass" not in seen[0]


def test_nudge_names_fixed_failures_and_rewritten_tests(parts):
    cfg, store, _, _ = parts
    run, proposal = _ready_run(store, {
        **GATE, "existing_failures_fixed": ["test_a"],
        "tests_rewritten": [{"test": "test_b", "reason": "old structure"}]})
    text = DeliveryManager(cfg, store, lambda p: "")._render_nudge(run, proposal)
    assert "1 existing test(s) that fail on your code now pass: test_a." in text
    assert "CHECK THESE: it changed existing test(s): test_b (old structure)." in text


# ---------------------------------------------------------------- end to end

def test_treatment_diff_delivery(counters_repo):
    task, repo, tmp_path = counters_repo
    agent = ReviewingAgent(repo, counter_agent_script())
    sail = FakeSailClient()
    episode = EvalEpisode(
        task, repo, main_agent=agent, hindsight=True,
        hindsight_home=tmp_path / "hs", sail=sail,
        model_fn=fake_model_fn,
        judge_model_fn=fake_judge_fn,
        settings_overrides={**FAST, "quiet_timeout": 60},
        relay_nudges=True, relay_diff=True, arm="treatment_diff")

    result = episode.run()

    assert result.nudge_count >= 1
    message = next(m for m in agent.messages if m.startswith(DELIVERY_MARKER))
    assert "THE DIFF" in message
    assert "diff --git a/counter.py b/counter.py" in message
    assert "No failing test prompted it" in message
    assert "previously-failing" not in message

    store = Store(tmp_path / "hs" / "events.sqlite3")
    try:
        runs = store._query("SELECT * FROM runs ORDER BY created_at")
        assert runs
        # Stage 1's request is the task; the first run answers stage 2's.
        assert json.loads(runs[0]["target_requirement_ids_json"]) == ["T2"]
    finally:
        store.close()
    worker_task = json.loads(sail.written_files["/work/task.json"])
    assert task.stages[0].message.strip() in worker_task["prompt"]
    assert task.stages[1].message.strip() in worker_task["prompt"]


def _prompt(strategy, **extra):
    from hindsight.executor import render_audit_worker_prompt
    return render_audit_worker_prompt(["build it"], strategy, "pytest", **extra)


def test_workers_must_redesign_and_cant_leave_the_code_alone():
    for prompt in (_prompt("open_redesign"), _prompt("direction_2", direction="split it")):
        assert "Restructure the code into your design" in prompt
        assert "leave the product code alone" not in prompt
        assert "{task}" not in prompt
    assert "split it" in _prompt("direction_2", direction="split it")


def test_final_worker_finishes_the_chosen_design_rather_than_its_own():
    prompt = _prompt("council_synthesis", directive="THE PLAN")
    assert "THE PLAN" in prompt and "finish the chosen redesign" in prompt
    assert "Don't start again" in prompt and "Keep the design's structure" in prompt
    assert "makes better than the old one" in prompt
    assert "Restructure the code into your design" not in prompt


def test_directions_are_parsed_capped_and_never_invented():
    from hindsight.design_review import MAX_DIRECTIONS, parse_directions, plan_directions

    item = lambda i: {"name": f"n{i}", "structure": f"s{i}", "why": f"w{i}"}  # noqa: E731
    got = parse_directions(json.dumps({"directions": [item(i) for i in range(9)]}))
    assert len(got) == MAX_DIRECTIONS
    assert got[0] == "n0: s0\nServes: w0"
    assert parse_directions('{"directions": [{"name": "no structure"}, "junk"]}') == []
    assert plan_directions("p", lambda p: "not json") == []

    def broken(prompt):
        raise RuntimeError("model down")

    assert plan_directions("p", broken) == []
