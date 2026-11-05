"""Council and final-worker tests.

Covers: ranking parsing + aggregation, critique storage, chairman parsing, how
designs become one result (final worker, its triggers, fallbacks, sending a new
message back to the winner), and the one-run-per-session rule.

All Sail/model/executor dependencies are injected fakes (UNIT TEST MOCKS).
"""

from __future__ import annotations

import json
import time

import pytest

from hindsight.config import HindsightConfig
from hindsight.council import (
    Council, calculate_aggregate_rankings, parse_ranking_from_text,
    proposal_brief)
from hindsight.selection import Selector
from hindsight.store import Store, built_on_key

from test_selection import (  # reuse the selection fixture harness
    FakeExecutor, SelectionSail, add_gating_worker, make_selector, parts, seed_run)

TWO_DIFFS = [{"files": 1, "added": 10, "removed": 2},
             {"files": 2, "added": 20, "removed": 3}]


# ------------------------------------------------------------------- parsing

RANKED_ANSWER = (
    "Proposal A rewrites everything elegantly but ignores the cache "
    "requirement...\nProposal B is a narrow patch...\n"
    "Proposal C combines the rewrite's shape with discipline.\n\n"
    "FINAL RANKING:\n1. Proposal C\n2. Proposal A\n3. Proposal B\n")


def test_parse_ranking_numbered_list():
    assert parse_ranking_from_text(RANKED_ANSWER, 3) == ["C", "A", "B"]


def test_parse_ranking_fallback_mention_order():
    text = "FINAL RANKING:\nProposal B then Proposal A then Proposal C"
    assert parse_ranking_from_text(text, 3) == ["B", "A", "C"]


def test_parse_ranking_no_header_scans_whole_text():
    text = "I would put Proposal B first, then Proposal C, then Proposal A."
    assert parse_ranking_from_text(text, 3) == ["B", "C", "A"]


def test_parse_ranking_garbage_is_empty():
    assert parse_ranking_from_text("no labels anywhere here", 3) == []


def test_parse_ranking_dedupes_and_drops_unknown_labels():
    text = "FINAL RANKING:\n1. Proposal A\n2. Proposal A\n3. Proposal Z"
    assert parse_ranking_from_text(text, 2) == ["A"]


def test_aggregate_rankings_average_positions():
    label_map = {"A": "p1", "B": "p2", "C": "p3"}
    critiques = [{"ranking": ["A", "B", "C"]}, {"ranking": ["B", "C"]}]
    agg = calculate_aggregate_rankings(critiques, label_map)
    # p1: (1 + unranked) -> avg 1.0 over 1 ranking; p2: (2+1)/2 = 1.5.
    assert agg[0] == {"proposal_id": "p1", "average_rank": 1.0,
                      "rankings_count": 1}
    assert agg[1] == {"proposal_id": "p2", "average_rank": 1.5,
                      "rankings_count": 2}
    # p3: positions (3, 2) across the two critics -> avg 2.5, sorts last.
    assert agg[-1] == {"proposal_id": "p3", "average_rank": 2.5,
                       "rankings_count": 2}


def test_proposal_brief_anonymized_content():
    p = {"id": "H-x", "summary": {
                "addresses": ["R1"], "summary": "made it shared",
                "risks": "none", "assumptions": [{"assumption": "gil held"}]},
         "diff_stat": {"files": 2, "added": 30, "removed": 4},
         "patch": "--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-x=1\n+x=2\n"}
    brief = proposal_brief("B", p)
    assert "### Proposal B" in brief
    assert "H-x" not in brief  # proposal identity stays anonymous
    assert "made it shared" in brief and "R1" in brief and "+30/-4" in brief
    assert "gil held" in brief


def test_proposal_brief_survives_unexpected_summary_shapes():
    """A worker's summary listed what it addresses as dicts, which crashed the
    review and left the run open (2026-11-05)."""
    for addresses in ([{"id": "R1", "why": "x"}], "R1", [1, None]):
        p = {"id": "H-y", "summary": {"addresses": addresses, "summary": "s"},
             "patch": ""}
        brief = proposal_brief("A", p)
        assert "### Proposal A" in brief


def test_chairman_parse():
    out = Council._parse_chairman("blah rationale\n###DIRECTIVE\ndo this", [])
    assert out["rationale"] == "blah rationale"
    assert out["directive"] == "do this"
    assert Council._parse_chairman("no header", []) is None
    assert Council._parse_chairman("###DIRECTIVE\n", []) is None


# --------------------------------------------------------------------- store

@pytest.fixture()
def store(tmp_path):
    s = Store(tmp_path / "db.sqlite3")
    yield s
    s.close()


def test_critique_and_chairman_roundtrip(store):
    run_id = store.create_run("s1", 1, ["R1"])
    store.save_critique(run_id, "requirements auditor", {"A": "H-1"},
                        ["A"], "raw text", model="Kimi-K3")
    rows = store.run_critiques(run_id)
    assert len(rows) == 1 and rows[0]["reviewer"] == "requirements auditor"
    assert json.loads(rows[0]["labels_json"]) == {"A": "H-1"}

    assert store.get_run_chairman(run_id) is None
    store.set_run_chairman(run_id, {"rationale": "r", "directive": "d",
                                    "aggregate": []})
    assert store.get_run_chairman(run_id)["directive"] == "d"


def test_one_active_run_per_session(store):
    assert not store.session_has_active_run("s1")
    run_id = store.create_run("s1", 1, ["R1"])  # NEW
    assert store.session_has_active_run("s1")
    # Another session is unaffected.
    assert not store.session_has_active_run("s2")
    store.transition_run(run_id, "PLANNED", "t")
    store.transition_run(run_id, "LAUNCHED", "t")
    store.transition_run(run_id, "SELECTING", "t")
    assert store.session_has_active_run("s1")
    # Ongoing until sent: waiting to deliver still blocks.
    store.transition_run(run_id, "READY_TO_DELIVER", "t")
    assert store.session_has_active_run("s1")
    store.transition_run(run_id, "DELIVERED", "t")
    assert not store.session_has_active_run("s1")
    # A corpse (stuck non-terminal beyond the grace window) does not block.
    corpse = store.create_run("s1", 1, ["R2"])
    store._exec("UPDATE runs SET created_at=? WHERE id=?",
                (time.time() - 25 * 3600, corpse))
    assert not store.session_has_active_run("s1")


def test_user_messages_since(store):
    turn_id = store.start_turn("s1", 1, "m1", "pre1")
    store.add_messages("s1", turn_id, [
        {"id": "m1", "role": "user", "text": "first"},
        {"id": "m2", "role": "assistant", "text": "answer"},
        {"id": "m3", "role": "user", "text": "second"}])
    msgs = store.user_messages_since("s1", 0.0)
    assert [m["text"] for m in msgs] == ["first", "second"]
    later = store.user_messages_since("s1", msgs[0]["ts"])
    assert [m["text"] for m in later] == ["second"]
    assert store.user_messages_since("s1", msgs[-1]["ts"]) == []


# ------------------------------------------------------------ deciding (e2e)

USER_WORDS = "please make the total include tax"


def add_user_message(store, text=USER_WORDS, msg_id="m-u", n=2):
    turn_id = store.start_turn("s1", n, msg_id, "pre")
    store.add_messages("s1", turn_id, [
        {"id": msg_id, "role": "user", "text": text, "ts": time.time()}])
    store.complete_turn(turn_id, "ok", "post")


def scripted_council_model(prompt: str) -> str:
    """UNIT TEST MOCK: every lens ranks Proposal B first; the chairman writes a
    plan."""
    if "Your specific lens" in prompt:
        return ("Proposal A looks rushed; Proposal B is careful.\n\n"
                "FINAL RANKING:\n1. Proposal B\n2. Proposal A\n")
    if "You are the chairman" in prompt:
        return ("RATIONALE: B's shape, A's helper.\n\n###DIRECTIVE\nMove counts "
                "into a module-level registry; run the suite.")
    raise AssertionError(f"unexpected council prompt: {prompt[:80]}")


class FakeRunManager:
    """UNIT TEST MOCK: records final-worker launches."""

    def __init__(self, exploration_id="ex-synth"):
        self.exploration_id = exploration_id
        self.launches = []

    def launch_synthesis(self, run, base_commit, plan, start_patch=None):
        self.launches.append({"run_id": run["id"], "plan": plan,
                              "base_commit": base_commit, "start_patch": start_patch})
        return self.exploration_id


def make_council_selector(cfg, store, snap, sail, runs=None, model=None,
                          executor=None) -> Selector:
    sel = make_selector(cfg, store, snap, sail, executor=executor)
    sel.council = Council(cfg, store, model or scripted_council_model,
                          model_name="Kimi-K3")
    sel.runs = runs or FakeRunManager()
    return sel


def add_final(store, run_id, *, summary="final", **kw) -> str:
    return add_gating_worker(
        store, run_id, summary=summary,
        budget={"target_ids": ["R1"], "synthesis": True}, **kw)


def failed_final(store, run_id) -> None:
    ex_id = store.create_exploration(
        "s1", "council_synthesis", "opencode", "t1", 1,
        {"target_ids": ["R1"], "synthesis": True}, run_id=run_id)
    store.transition_exploration(ex_id, "PROVISIONING", "seed")
    store.transition_exploration(ex_id, "RUNNING", "seed")
    store.transition_exploration(ex_id, "FAILED", "worker crashed")


def _run_reason(store, run_id, state="READY_TO_DELIVER") -> str:
    row = store._query_one(
        "SELECT reason FROM run_events WHERE run_id=? AND to_state=?",
        (run_id, state))
    return row["reason"] if row else ""


def seed_two_with_boxes(store) -> tuple[str, str, str]:
    run_id = store.create_run("s1", 1, ["R1"])
    store.transition_run(run_id, "PLANNED", "seed")
    a = add_gating_worker(store, run_id, box_id="worker-a", diff_stat=TWO_DIFFS[0])
    b = add_gating_worker(store, run_id, box_id="worker-b", diff_stat=TWO_DIFFS[1])
    store.transition_run(run_id, "LAUNCHED", "seed")
    return run_id, a, b


def test_several_designs_go_through_chairman_to_final_worker(parts):
    cfg, store, snap, commit = parts
    add_user_message(store)
    run_id = seed_run(store, n_proposals=2, diff_stats=TWO_DIFFS)
    # These messages predate the designs: nothing here is "unseen".
    runs = FakeRunManager()
    prompts: list[str] = []

    def model(prompt):
        prompts.append(prompt)
        return scripted_council_model(prompt)

    sel = make_council_selector(cfg, store, snap, SelectionSail(), runs, model)
    sel.on_exploration_finished(run_id)

    assert store.get_run(run_id)["state"] == "SELECTING"
    assert len(runs.launches) == 1
    assert "module-level registry" in runs.launches[0]["plan"]
    # Critics and chairman read the user's verbatim words.
    assert len(prompts) == 4
    assert all(USER_WORDS in p for p in prompts)
    assert {c["reviewer"] for c in store.run_critiques(run_id)} == \
        {"requirements auditor", "simplicity advocate", "scope examiner"}
    chairman = store.get_run_chairman(run_id)
    designs = store.run_proposals(run_id)
    assert chairman["aggregate"][0] == {
        "proposal_id": designs[1]["id"], "average_rank": 1.0, "rankings_count": 3}

    # The final worker's result qualifies and is what gets sent.
    add_final(store, run_id, summary="the final")
    sel.on_exploration_finished(run_id)
    run = store.get_run(run_id)
    assert run["state"] == "READY_TO_DELIVER"
    assert store.get_proposal(run["winner_proposal_id"])["summary"]["summary"] == "the final"


def test_critics_can_be_turned_off(parts):
    cfg, store, snap, commit = parts
    cfg.council.reviewers = 0
    run_id = seed_run(store, n_proposals=2, diff_stats=TWO_DIFFS)
    prompts: list[str] = []

    def model(prompt):
        prompts.append(prompt)
        return scripted_council_model(prompt)

    runs = FakeRunManager()
    make_council_selector(cfg, store, snap, SelectionSail(), runs, model
                          ).on_exploration_finished(run_id)
    assert len(prompts) == 1 and "You are the chairman" in prompts[0]
    assert store.run_critiques(run_id) == []
    assert len(runs.launches) == 1


def test_winner_keeps_its_box_and_losers_lose_theirs(parts):
    cfg, store, snap, commit = parts
    sail = SelectionSail()
    run_id, a, b = seed_two_with_boxes(store)
    sel = make_council_selector(cfg, store, snap, sail)
    sel.on_exploration_finished(run_id)

    add_final(store, run_id, box_id="worker-final")
    sel.on_exploration_finished(run_id)

    assert store.get_run(run_id)["state"] == "READY_TO_DELIVER"
    assert {"worker-a", "worker-b"} <= set(sail.terminated)
    assert "worker-final" not in sail.terminated


def test_run_that_ends_empty_frees_all_its_boxes_and_no_others(parts):
    cfg, store, snap, commit = parts
    sail = SelectionSail()
    other = store.create_run("s1", 1, ["R9"])
    store.transition_run(other, "PLANNED", "seed")
    add_gating_worker(store, other, box_id="other-run")
    run_id, a, b = seed_two_with_boxes(store)

    make_council_selector(cfg, store, snap, sail, model=lambda p: "no directive"
                          ).on_exploration_finished(run_id)

    assert store.get_run(run_id)["state"] == "NONE_QUALIFIED"
    assert {"worker-a", "worker-b"} <= set(sail.terminated)
    assert "other-run" not in sail.terminated


def test_final_worker_failure_sends_nothing(parts):
    cfg, store, snap, commit = parts
    run_id = seed_run(store, n_proposals=2, diff_stats=TWO_DIFFS)
    sel = make_council_selector(cfg, store, snap, SelectionSail())
    sel.on_exploration_finished(run_id)

    failed_final(store, run_id)
    sel.on_exploration_finished(run_id)

    run = store.get_run(run_id)
    assert run["state"] == "NONE_QUALIFIED"
    assert run["winner_proposal_id"] is None


def test_chairman_unavailable_sends_nothing(parts):
    cfg, store, snap, commit = parts
    run_id = seed_run(store, n_proposals=2, diff_stats=TWO_DIFFS)
    runs = FakeRunManager()
    sel = make_council_selector(cfg, store, snap, SelectionSail(), runs,
                                model=lambda p: "no directive here")
    sel.on_exploration_finished(run_id)

    assert store.get_run(run_id)["state"] == "NONE_QUALIFIED"
    assert runs.launches == []


def test_final_worker_refused_by_budget_sends_nothing(parts):
    cfg, store, snap, commit = parts
    run_id = seed_run(store, n_proposals=2, diff_stats=TWO_DIFFS)
    sel = make_council_selector(cfg, store, snap, SelectionSail(),
                                FakeRunManager(exploration_id=None))
    sel.on_exploration_finished(run_id)
    assert store.get_run(run_id)["state"] == "NONE_QUALIFIED"


# ---------------------------------------------------- when one design remains

def test_lone_design_still_goes_through_the_council(parts):
    """Workers can't say the code should stay as it is, so someone else must get to."""
    cfg, store, snap, commit = parts
    run_id = seed_run(store, n_proposals=1)
    runs = FakeRunManager()
    prompts: list[str] = []

    def model(prompt):
        prompts.append(prompt)
        return scripted_council_model(prompt)

    make_council_selector(cfg, store, snap, SelectionSail(), runs,
                          model).on_exploration_finished(run_id)

    assert prompts
    assert store.get_run(run_id)["state"] == "SELECTING"
    assert len(runs.launches) == 1


@pytest.mark.parametrize("n_proposals", [1, 2])
def test_chairman_can_send_nothing(parts, n_proposals):
    cfg, store, snap, commit = parts
    run_id = seed_run(store, n_proposals=n_proposals, diff_stats=TWO_DIFFS)
    runs = FakeRunManager()

    def model(prompt):
        if "CHAIRMAN" in prompt.upper()[:60]:
            return "###NO_CHANGE\nEach design only moves code around."
        return scripted_council_model(prompt)

    make_council_selector(cfg, store, snap, SelectionSail(), runs,
                          model).on_exploration_finished(run_id)

    run = store.get_run(run_id)
    assert run["state"] == "NONE_QUALIFIED"
    assert "only moves code around" in run["exit_reason"]
    assert runs.launches == []


def test_failing_design_stays_a_candidate_and_critics_see_its_tests(parts):
    cfg, store, snap, commit = parts
    run_id = seed_run(store, n_proposals=2, diff_stats=TWO_DIFFS)
    for p in store.run_proposals(run_id):
        store.save_gate_result(p["id"], "1_worker_tests", True,
                               {"passed": 3, "failed": 2}, "recorded")
    prompts: list[str] = []

    def model(prompt):
        prompts.append(prompt)
        return scripted_council_model(prompt)

    runs = FakeRunManager()
    make_council_selector(cfg, store, snap, SelectionSail(proposal={"test_app": "fail"}),
                          runs, model).on_exploration_finished(run_id)

    assert len(runs.launches) == 1
    assert all("3 passed, 2 failed" in p for p in prompts)


def test_final_design_that_breaks_a_test_gets_one_retry_with_the_failures(parts):
    cfg, store, snap, commit = parts
    sail = SelectionSail(proposal={"test_app": "fail"})
    runs = FakeRunManager()
    run_id = store.create_run("s1", 1, ["R1"])
    store.transition_run(run_id, "PLANNED", "seed")
    add_final(store, run_id)
    store.transition_run(run_id, "LAUNCHED", "seed")
    sel = make_council_selector(cfg, store, snap, sail, runs)

    sel.on_exploration_finished(run_id)

    assert store.get_run(run_id)["state"] == "SELECTING"
    assert len(runs.launches) == 1
    assert "test_app" in runs.launches[0]["plan"]

    add_final(store, run_id, summary="second try")
    sel.on_exploration_finished(run_id)

    assert store.get_run(run_id)["state"] == "READY_TO_DELIVER"
    assert len(runs.launches) == 1


def test_final_design_is_checked_on_its_own_base_not_the_latest_code(parts):
    """The agent moved on while it was built. That doesn't stop it being sent."""
    cfg, store, snap, commit = parts
    run_id = seed_run(store, n_proposals=1)
    runs = FakeRunManager()
    sail = SelectionSail()
    sel = make_council_selector(cfg, store, snap, sail, runs)
    sel.on_exploration_finished(run_id)
    assert len(runs.launches) == 1

    final = add_final(store, run_id, summary="final")
    ex_id = store.get_proposal(final)["exploration_id"]
    store.kv_set(built_on_key(ex_id), "older-commit")
    seen = []
    real = sel._fresh_box
    sel._fresh_box = lambda name, base: (seen.append(base), real(name, commit))[1]
    sel.on_exploration_finished(run_id)

    assert seen and set(seen) == {"older-commit"}
    assert store.get_run(run_id)["state"] == "READY_TO_DELIVER"

def test_empty_test_result_never_qualifies(parts):
    """No executed evidence, and no command of its own to run, is not a pass."""
    cfg, store, snap, commit = parts

    class EmptySail(SelectionSail):
        def read_file(self, box_id, path):  # junit never appears
            return b""

    run_id = store.create_run("s1", 1, ["R1"])
    store.transition_run(run_id, "PLANNED", "seed")
    add_final(store, run_id)
    store.transition_run(run_id, "LAUNCHED", "seed")
    store.kv_set(f"final_retried:{run_id}", "1")
    sel = make_council_selector(cfg, store, snap, EmptySail())
    sel.on_exploration_finished(run_id)
    assert store.get_run(run_id)["state"] == "NONE_QUALIFIED"
    reasons = {r["reason"] for r in store._query(
        "SELECT reason FROM gate_results WHERE stage='2_audit_gate'")}
    assert reasons == {"no project tests; no verify_command given"}


# ------------------------------------------------------------------ sending back

def ready_run(store, sel, *, box_id="worker-a") -> tuple[str, dict]:
    run_id = store.create_run("s1", 1, ["R1"])
    store.transition_run(run_id, "PLANNED", "seed")
    add_gating_worker(store, run_id, box_id=box_id,
                      budget={"target_ids": ["R1"], "synthesis": True})
    store.transition_run(run_id, "LAUNCHED", "seed")
    sel.on_exploration_finished(run_id)
    run = store.get_run(run_id)
    assert run["state"] == "READY_TO_DELIVER"
    return run_id, store.get_proposal(run["winner_proposal_id"])


def test_message_after_the_winner_finished_goes_back_to_that_worker(parts):
    from hindsight.delivery import DeliveryManager

    cfg, store, snap, commit = parts
    cfg.experiments.late_messages = "retry_final"
    sail = SelectionSail()
    executor = FakeExecutor()
    sel = make_council_selector(cfg, store, snap, sail, executor=executor,
                                model=lambda p: pytest.fail("not the chairman"))
    run_id, proposal = ready_run(store, sel)
    delivery = DeliveryManager(cfg, store, lambda p: "nudge")
    delivery.selector = sel
    add_user_message(store, "also round to cents")

    decisions = delivery.maybe_deliver("s1")

    assert decisions[0]["outcome"] == "sent_back"
    assert store.nudges_shown_count("s1") == 0
    assert store.get_run(run_id)["state"] == "SELECTING"
    assert sail.woken == ["worker-a"]
    assert store.exploration_state(proposal["exploration_id"]) == "SUPERSEDED"
    ex_id, messages = executor.followups[0]
    assert [m["text"] for m in messages] == ["also round to cents"]
    assert store.get_exploration(ex_id)["box_id"] == "worker-a"
    assert "worker-a" not in sail.terminated
    # Its new result then passes the same gate and is what gets sent.
    new_id = store.save_proposal(ex_id, proposal["patch"], {"summary": "updated"},
                                 {"files": 1})
    for state in ("PROVISIONING", "RUNNING", "TESTING", "GATING"):
        store.transition_exploration(ex_id, state, "seed")
    store.kv_set(f"updates_wm:{ex_id}", str(store.last_user_message_id("s1")))
    sel.on_exploration_finished(run_id)
    run = store.get_run(run_id)
    assert run["state"] == "READY_TO_DELIVER" and run["winner_proposal_id"] == new_id
    assert delivery.maybe_deliver("s1")[0]["outcome"] == "delivered"


def test_send_back_starts_a_final_worker_when_the_box_will_not_wake(parts):
    cfg, store, snap, commit = parts
    sail = SelectionSail()
    runs = FakeRunManager()
    executor = FakeExecutor()
    sel = make_council_selector(cfg, store, snap, sail, runs, executor=executor)
    run_id, proposal = ready_run(store, sel)
    add_user_message(store, "also round to cents")
    sail.wake_fails = True

    sel.send_back(store.get_run(run_id), proposal,
                  store.messages_unseen_by(proposal["exploration_id"]))

    assert executor.followups == []
    assert len(runs.launches) == 1
    assert store.get_run(run_id)["state"] == "SELECTING"


def test_send_back_is_bounded(parts):
    from hindsight.selection import MAX_FINAL_ROUNDS

    cfg, store, snap, commit = parts
    sail = SelectionSail()
    executor = FakeExecutor()
    sel = make_council_selector(cfg, store, snap, sail, executor=executor)
    run_id, proposal = ready_run(store, sel)
    for _ in range(MAX_FINAL_ROUNDS):
        failed_final(store, run_id)
    add_user_message(store, "yet another change")

    sel.send_back(store.get_run(run_id), proposal,
                  store.messages_unseen_by(proposal["exploration_id"]))

    assert executor.followups == []
    assert store.get_run(run_id)["state"] == "DROPPED_STALE"
    assert "worker-a" in sail.terminated


def test_message_stored_after_the_worker_finished_is_unseen_despite_older_ts(parts):
    """A message is stored when its turn ends, so its timestamp can predate a
    worker's finish without the worker ever having seen it."""
    cfg, store, snap, commit = parts
    run_id = store.create_run("s1", 1, ["R1"])
    pid = add_gating_worker(store, run_id)
    p = store.get_proposal(pid)
    store.kv_set(f"updates_wm:{p['exploration_id']}",
                 str(store.last_user_message_id("s1")))
    turn_id = store.start_turn("s1", 2, "m9", "pre")
    store.add_messages("s1", turn_id, [
        {"id": "m9", "role": "user", "text": "sent long ago", "ts": 1.0}])
    assert [m["text"] for m in store.messages_unseen_by(p["exploration_id"])] == \
        ["sent long ago"]


# --------------------------------------------------------- updates stream


class UpdatesSail:
    """UNIT TEST MOCK: captures file pushes; can simulate a box error."""

    def __init__(self):
        self.written: dict[str, str] = {}
        self.fail_writes = False

    def write_files(self, box_id, files):
        if self.fail_writes:
            from hindsight.sail_client import SailClientError
            raise SailClientError("box down")
        self.written.update(files)


def add_turn(store, n, messages):
    turn_id = store.start_turn("s1", n, f"m{n}", "pre")
    store.add_messages("s1", turn_id, [
        {"id": f"m{n}-{i}", "role": role, "text": text, "ts": time.time()}
        for i, (role, text) in enumerate(messages)])


def _updates_executor(tmp_path, sail):
    from hindsight.executor import Executor
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    cfg = HindsightConfig(project_dir=repo, hindsight_home=tmp_path / "hs")
    store = Store(cfg.db_path)
    ex_id = store.create_exploration("s1", "clean_rewrite", "pi", "t1", 1,
                                     {"target_ids": []}, run_id=None)
    return Executor(cfg, store, None, sail, None), store, ex_id


def test_worker_updates_streamed_and_watermarked(tmp_path):
    sail = UpdatesSail()
    ex, store, ex_id = _updates_executor(tmp_path, sail)
    # First turn AFTER the exploration exists: its user text is an update.
    add_turn(store, 1, [("user", "also make it green"),
                        ("assistant", "ack")])
    ex._push_updates(ex_id, "box-1")
    assert len(sail.written) == 1
    path, body = next(iter(sail.written.items()))
    assert path.startswith("/work/updates/u-")
    assert json.loads(body)["text"] == "also make it green"

    # Watermarked: the same poll pushes nothing new.
    ex._push_updates(ex_id, "box-1")
    assert len(sail.written) == 1

    # The next user message arrives as a second update file.
    add_turn(store, 2, [("user", "and keep it under 50 lines")])
    ex._push_updates(ex_id, "box-1")
    assert len(sail.written) == 2


def test_worker_updates_never_collide_on_same_ts(tmp_path):
    """Regression: update files were named u-{ts_ms}.json —
    two user messages in the SAME millisecond collided, silently overwriting
    a message the worker then never saw. Files are now keyed by the message
    row id, so every message always gets its own file."""
    sail = UpdatesSail()
    ex, store, ex_id = _updates_executor(tmp_path, sail)
    turn_id = store.start_turn("s1", 1, "m1", "pre1")
    same_ts = time.time()
    store.add_messages("s1", turn_id, [
        {"id": "m1-a", "role": "user", "text": "first same-ms",
         "ts": same_ts},
        {"id": "m1-b", "role": "user", "text": "second same-ms",
         "ts": same_ts}])
    ex._push_updates(ex_id, "box-1")
    assert len(sail.written) == 2
    texts = {json.loads(b)["text"] for b in sail.written.values()}
    assert texts == {"first same-ms", "second same-ms"}


def test_worker_updates_flag_off(tmp_path):
    sail = UpdatesSail()
    ex, store, ex_id = _updates_executor(tmp_path, sail)
    ex.cfg.council.stream_worker_updates = False
    add_turn(store, 1, [("user", "new ask")])
    ex._push_updates(ex_id, "box-1")
    assert sail.written == {}


def test_worker_updates_box_error_keeps_watermark(tmp_path):
    sail = UpdatesSail()
    ex, store, ex_id = _updates_executor(tmp_path, sail)
    add_turn(store, 1, [("user", "msg one")])
    sail.fail_writes = True
    ex._push_updates(ex_id, "box-1")
    assert sail.written == {}
    sail.fail_writes = False
    ex._push_updates(ex_id, "box-1")
    assert len(sail.written) == 1  # retry delivered the same message


# ---------------------------------------------------------- no project tests

from hindsight.sail_client import ExecOut  # noqa: E402


class VerifySail(SelectionSail):
    """Reports the exit code of the verify command; records what ran."""

    def __init__(self, verify_exit=0, **kw):
        super().__init__(**kw)
        self.verify_exit = verify_exit
        self.commands: list[str] = []

    def run(self, box_id, command, *, timeout=None, cwd=None, env=None):
        cmd = " ".join(command)
        self.commands.append(cmd)
        if "make check" in cmd:
            return ExecOut(self.verify_exit, "", "boom", False)
        return super().run(box_id, command, timeout=timeout, cwd=cwd, env=env)


GO_PATCH = ("diff --git a/app.go b/app.go\n--- a/app.go\n+++ b/app.go\n"
            "@@ -1 +1 @@\n-a\n+b\n"
            "diff --git a/app_test.go b/app_test.go\n--- a/app_test.go\n"
            "+++ b/app_test.go\n@@ -1 +1 @@\n-a\n+b\n")


def final_without_suite(store, *, patch=GO_PATCH, verify="make check", retried=True):
    run_id = store.create_run("s1", 1, ["R1"])
    store.transition_run(run_id, "PLANNED", "seed")
    add_final(store, run_id, patch=patch,
              summary={"summary": "final", "verify_command": verify})
    store.transition_run(run_id, "LAUNCHED", "seed")
    if retried:
        store.kv_set(f"final_retried:{run_id}", "1")
    return run_id


def test_no_project_tests_passes_on_its_own_build_and_tests(parts):
    cfg, store, snap, commit = parts
    cfg.project.test_command = ""
    sail = VerifySail()
    run_id = final_without_suite(store)

    make_council_selector(cfg, store, snap, sail).on_exploration_finished(run_id)

    run = store.get_run(run_id)
    assert run["state"] == "READY_TO_DELIVER"
    assert any("make check" in c for c in sail.commands)
    gate = store.gate_features(run["winner_proposal_id"], "2_audit_gate")
    assert gate["no_project_suite"] is True and gate["verify_command"] == "make check"


def test_no_project_tests_is_named_in_what_the_agent_reads(parts):
    from hindsight.delivery import DeliveryManager

    cfg, store, snap, commit = parts
    cfg.project.test_command = ""
    run_id = final_without_suite(store)
    make_council_selector(cfg, store, snap, VerifySail()).on_exploration_finished(run_id)
    dm = DeliveryManager(cfg, store, lambda p: "An alternative is available.")
    dm.maybe_deliver("s1")
    text = store.pending_agent_deliveries("s1")[0]["text"]
    assert "no project test suite" in text and "make check" in text
    assert "with no regressions" not in text


def test_failing_its_own_check_gets_the_retry_with_the_failure(parts):
    cfg, store, snap, commit = parts
    cfg.project.test_command = ""
    runs = FakeRunManager()
    run_id = final_without_suite(store, retried=False)
    make_council_selector(cfg, store, snap, VerifySail(verify_exit=2), runs
                          ).on_exploration_finished(run_id)
    assert store.get_run(run_id)["state"] == "SELECTING"
    assert "`make check` exited 2" in runs.launches[0]["plan"]


def test_failing_its_own_tests_after_the_retry_is_sent_with_the_failure(parts):
    from hindsight.delivery import DeliveryManager

    cfg, store, snap, commit = parts
    cfg.project.test_command = ""
    run_id = final_without_suite(store)
    make_council_selector(cfg, store, snap, VerifySail(verify_exit=2)
                          ).on_exploration_finished(run_id)
    assert store.get_run(run_id)["state"] == "READY_TO_DELIVER"
    DeliveryManager(cfg, store, lambda p: "An alternative.").maybe_deliver("s1")
    text = store.pending_agent_deliveries("s1")[0]["text"]
    assert "CHECK THESE" in text and "`make check` exited 2" in text


@pytest.mark.parametrize("patch,verify,why", [
    (GO_PATCH, "", "no verify_command"),
    ("diff --git a/app.go b/app.go\n--- a/app.go\n+++ b/app.go\n@@ -1 +1 @@\n-a\n+b\n",
     "make check", "adds no tests"),
])
def test_no_project_tests_needs_a_command_and_tests(parts, patch, verify, why):
    cfg, store, snap, commit = parts
    cfg.project.test_command = ""
    run_id = final_without_suite(store, patch=patch, verify=verify)
    make_council_selector(cfg, store, snap, VerifySail()).on_exploration_finished(run_id)
    run = store.get_run(run_id)
    assert run["state"] == "NONE_QUALIFIED"
    assert why in store.run_proposals(run_id)[0]["regressions"][0]


def test_a_project_command_without_results_needs_no_new_tests(parts):
    cfg, store, snap, commit = parts
    cfg.project.test_command = "make check"
    patch = "diff --git a/app.go b/app.go\n--- a/app.go\n+++ b/app.go\n@@ -1 +1 @@\n-a\n+b\n"

    class NoJunit(VerifySail):
        def read_file(self, box_id, path):
            from hindsight.sail_client import SailClientError
            if "junit" in path:
                raise SailClientError("no junit")
            return super().read_file(box_id, path)

    run_id = final_without_suite(store, patch=patch)
    make_council_selector(cfg, store, snap, NoJunit()).on_exploration_finished(run_id)
    assert store.get_run(run_id)["state"] == "READY_TO_DELIVER"


@pytest.mark.parametrize("path,is_test", [
    ("app_test.go", True), ("pkg/test_a.py", True), ("tests/run.sh", True),
    ("test/x.c", True), ("a.test.ts", True), ("suite.bats", True),
    ("latest.go", False), ("contest/main.go", False), ("testing.py", False),
])
def test_test_paths_across_languages(path, is_test):
    from hindsight.selection import _is_test_path
    assert _is_test_path(path) is is_test
