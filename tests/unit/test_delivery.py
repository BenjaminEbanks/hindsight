"""Delivery and dismissal-memory tests. All model dependencies are injected fakes (UNIT TEST MOCKS)."""

from __future__ import annotations

import time

import pytest

from hindsight.config import HindsightConfig
from hindsight.delivery import DeliveryManager, dismissal_fingerprint
from hindsight.store import Store


@pytest.fixture()
def env(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "app.py").write_text("x = 1\n")
    cfg = HindsightConfig(project_dir=repo, hindsight_home=tmp_path / "hs")
    store = Store(cfg.db_path)
    yield cfg, store
    store.close()


def nudge_fn(text="A background redesign is ready: shared counts now work."):
    def fn(prompt: str) -> str:
        fn.prompt = prompt
        return text

    fn.prompt = ""
    return fn


def seed_ready_run(store, *, session_id="s1", strategy="clean_rewrite",
                   assumptions=None) -> tuple[str, str]:
    """A READY_TO_DELIVER run with a winner proposal. Returns (run_id, proposal_id)."""
    run_id = store.create_run(session_id, 1, ["R1"])
    store.transition_run(run_id, "PLANNED", "seed")
    store.transition_run(run_id, "LAUNCHED", "seed")
    store.transition_run(run_id, "SELECTING", "seed")
    ex_id = store.create_exploration(
        session_id, strategy, "pi", "t1", 1, {"target_ids": ["R1"]},
        run_id=run_id)
    store.transition_exploration(ex_id, "PROVISIONING", "seed")
    store.transition_exploration(ex_id, "RUNNING", "seed")
    store.transition_exploration(ex_id, "TESTING", "seed")
    store.transition_exploration(ex_id, "GATING", "seed")
    store.transition_exploration(ex_id, "NUDGE_READY", "seed")
    summary = {"summary": "moved counts to a shared store"}
    if assumptions is not None:
        summary["assumptions"] = assumptions
    proposal_id = store.save_proposal(
        ex_id, "--- a/app.py\n+++ b/app.py\n", summary,
        {"files": 2, "added": 30, "removed": 4})
    store.set_proposal_selection(proposal_id, True, [], [])
    store.set_run_winner(run_id, proposal_id)
    store.transition_run(run_id, "READY_TO_DELIVER", "seed")
    # Recent activity so the user is not "away".
    turn_id = store.start_turn(session_id, 1, "m1", "c1")
    store.complete_turn(turn_id, "ok", "c1")
    return run_id, proposal_id


def test_delivers_with_checked_facts(env):
    cfg, store = env
    run_id, proposal_id = seed_ready_run(store)
    fn = nudge_fn()
    dm = DeliveryManager(cfg, store, fn)

    decisions = dm.maybe_deliver("s1")
    assert decisions == [{"run": run_id, "outcome": "delivered",
                           "proposal": proposal_id}]

    run = store.get_run(run_id)
    assert run["state"] == "DELIVERED"
    # The nudge rendered from the checked facts, not worker prose.
    assert "+30/-4 across 2 file(s)" in fn.prompt
    nudges = store._query("SELECT * FROM nudges")
    assert len(nudges) == 1
    assert nudges[0]["shown_at"] is not None
    assert "shared counts" in nudges[0]["text"]
    deliveries = store._query("SELECT * FROM deliveries")
    assert deliveries[0]["shown_at"] is not None
    assert deliveries[0]["held_reason"] == ""




def test_dismissal_is_remembered_for_the_next_design_review(env):
    from hindsight.design_review import run_dismissed_key

    cfg, store = env
    run_id, proposal_id = seed_ready_run(store)
    dm = DeliveryManager(cfg, store, nudge_fn())
    assert dm.maybe_deliver("s1")[0]["outcome"] == "delivered"

    dm.record_dismissal(proposal_id, "wrong_direction")

    assert store.kv_get(run_dismissed_key(run_id)) == "wrong_direction"




def test_nudge_render_falls_back_to_raw_facts(env):
    cfg, store = env
    seed_ready_run(store)

    def broken(prompt: str) -> str:
        raise RuntimeError("model down")

    dm = DeliveryManager(cfg, store, broken)
    decisions = dm.maybe_deliver("s1")
    assert decisions[0]["outcome"] == "delivered"
    text = store._query("SELECT * FROM nudges")[0]["text"]
    assert "An alternative version is ready: +30/-4 across 2 file(s)." in text


def test_missing_winner_drops_honestly(env):
    cfg, store = env
    run_id, _ = seed_ready_run(store)
    store._exec("UPDATE runs SET winner_proposal_id=NULL WHERE id=?", (run_id,))
    dm = DeliveryManager(cfg, store, nudge_fn())
    decisions = dm.maybe_deliver("s1")
    assert decisions[0]["outcome"] == "dropped"
    assert store.get_run(run_id)["state"] == "DROPPED_STALE"


def test_delivery_nudge_prompt_mentions_only_facts():
    from hindsight.prompts import load
    text = load("delivery_nudge_audit")
    assert "{reason}" in text and "{assumptions}" in text


def test_nudge_states_its_basis(env):
    """Basis framing:
    the receiving agent VERIFIES nudges against its repo, and a
    correct-but-stale nudge with no stated basis gets rejected whole.
    The nudge must name the conversation turn the workers built
    against so the reader reconciles instead of rejects."""
    cfg, store = env
    run_id, _ = seed_ready_run(store)
    # Give the exploration a REAL base turn: message #2 of the session.
    turn_id = store.start_turn("s1", 2, "m2", "c2")
    store.complete_turn(turn_id, "ok", "c2")
    store._exec(
        "UPDATE explorations SET base_turn_id=? WHERE session_id='s1'",
        (turn_id,))
    fn = nudge_fn()
    dm = DeliveryManager(cfg, store, fn)

    assert dm.maybe_deliver("s1")[0]["outcome"] == "delivered"
    assert "your code after conversation message #2" in fn.prompt


def test_nudge_fallback_carries_basis(env):
    """The honest fallback (model prose failed) still names the basis."""
    cfg, store = env
    run_id, _ = seed_ready_run(store)
    turn_id = store.start_turn("s1", 3, "m3", "c3")
    store.complete_turn(turn_id, "ok", "c3")
    store._exec(
        "UPDATE explorations SET base_turn_id=? WHERE session_id='s1'",
        (turn_id,))
    dm = DeliveryManager(cfg, store, nudge_fn(text=""))  # model failed

    assert dm.maybe_deliver("s1")[0]["outcome"] == "delivered"
    nudge = store._query("SELECT * FROM nudges")[0]
    assert "built against your code after conversation message #3" in nudge["text"]


def test_delivery_keeps_the_winners_box_for_questions(env):
    from hindsight.resume import WorkerResume

    cfg, store = env
    run_id, proposal_id = seed_ready_run(store)
    ex_id = store.get_proposal(proposal_id)["exploration_id"]
    store.set_box_id(ex_id, "worker-box")
    terminated = []

    class Sail:
        def terminate(self, box_id):
            terminated.append(box_id)

    dm = DeliveryManager(cfg, store, nudge_fn())
    dm.resume = WorkerResume(cfg, store, Sail())
    assert dm.maybe_deliver("s1")[0]["outcome"] == "delivered"
    assert store.kept_box("s1")["box_id"] == "worker-box"
    assert terminated == []


def test_new_message_is_never_sent_past(env):
    """retry_final: without a selector to send it back to, it holds rather
    than sends."""
    cfg, store = env
    cfg.experiments.late_messages = "retry_final"
    run_id, proposal_id = seed_ready_run(store)
    turn_id = store.start_turn("s1", 2, "m2", "c2")
    store.add_messages("s1", turn_id, [
        {"id": "m2", "role": "user", "text": "actually, no", "ts": time.time()}])
    store.complete_turn(turn_id, "ok", "c2")
    dm = DeliveryManager(cfg, store, nudge_fn())
    decisions = dm.maybe_deliver("s1")
    assert decisions[0]["reason"] == "newer_user_message"
    assert store.nudges_shown_count("s1") == 0
    assert store.get_run(run_id)["state"] == "READY_TO_DELIVER"


def test_new_message_is_listed_for_the_agent_by_default(env):
    cfg, store = env
    run_id, _ = seed_ready_run(store)
    turn_id = store.start_turn("s1", 2, "m2", "c2")
    store.add_messages("s1", turn_id, [
        {"id": "m2", "role": "user", "text": "actually, no", "ts": time.time()}])
    store.complete_turn(turn_id, "ok", "c2")
    dm = DeliveryManager(cfg, store, nudge_fn())

    assert dm.maybe_deliver("s1")[0]["outcome"] == "delivered"

    assert store.get_run(run_id)["state"] == "DELIVERED"
    [pending] = store.pending_agent_deliveries("s1")
    assert pending["missed"] == ["actually, no"]
    text = dm.agent_message(pending)
    assert "built before these messages" in text and "- actually, no" in text


def test_no_missed_section_when_nothing_was_missed(env):
    cfg, store = env
    seed_ready_run(store)
    dm = DeliveryManager(cfg, store, nudge_fn())
    dm.maybe_deliver("s1")
    [pending] = store.pending_agent_deliveries("s1")
    assert pending["missed"] == []
    assert "built before these messages" not in dm.agent_message(pending)


def test_agent_is_told_it_can_take_part_of_a_design(env):
    cfg, store = env
    seed_ready_run(store)
    dm = DeliveryManager(cfg, store, nudge_fn())
    dm.maybe_deliver("s1")
    text = dm.agent_message(store.pending_agent_deliveries("s1")[0])
    assert "all of it, part of it, only its ideas, or none" in text
    assert "otherwise make the edits yourself" in text
    assert "Do not change any code until the user agrees" in text


def test_two_designs_in_a_row_need_no_gap(env):
    cfg, store = env
    dm = DeliveryManager(cfg, store, nudge_fn())
    seed_ready_run(store)
    assert dm.maybe_deliver("s1")[0]["outcome"] == "delivered"
    seed_ready_run(store, strategy="refactor")
    assert dm.maybe_deliver("s1")[0]["outcome"] == "delivered"


def test_nothing_is_released_while_a_turn_is_going(env):
    """A message is stored when its turn ends, so mid-turn the unseen-message
    check cannot see it."""
    cfg, store = env
    run_id, _ = seed_ready_run(store)
    store.start_turn("s1", 2, "m2", "c2")
    dm = DeliveryManager(cfg, store, nudge_fn())

    decisions = dm.maybe_deliver("s1")

    assert decisions[0] == {"run": run_id, "outcome": "held",
                            "reason": "turn_in_progress"}
    assert store.get_run(run_id)["state"] == "READY_TO_DELIVER"
    assert store.nudges_shown_count("s1") == 0


def test_agent_message_says_what_it_is_and_forbids_applying(env):
    from hindsight.delivery import DELIVERY_MARKER, render_agent_message
    msg = render_agent_message("the facts", "diff --git a/a b/a", 4)
    assert msg.startswith(DELIVERY_MARKER)
    assert "not from the user" in msg
    assert "the facts" in msg and "diff --git a/a b/a" in msg and "#4" in msg
    assert "until the user agrees" in msg


def test_pending_agent_deliveries_until_sent(env):
    cfg, store = env
    seed_ready_run(store)
    DeliveryManager(cfg, store, nudge_fn()).maybe_deliver("s1")
    [item] = store.pending_agent_deliveries("s1")
    assert item["text"] and "a/app.py" in item["patch"]
    assert store.pending_agent_deliveries("other") == []
    assert store.mark_agent_delivery_sent(item["id"])
    assert store.pending_agent_deliveries("s1") == []
    assert not store.mark_agent_delivery_sent(item["id"])


def test_there_is_no_limit_on_deliveries(env):
    cfg, store = env
    dm = DeliveryManager(cfg, store, nudge_fn())
    for _ in range(5):
        seed_ready_run(store)
        assert dm.maybe_deliver("s1")[0]["outcome"] == "delivered"


def test_a_design_says_how_many_messages_behind_it_is():
    from hindsight.delivery import render_agent_message

    msg = render_agent_message("facts", "diff --git a/a b/a", 2, latest_turn_n=5)
    assert "3 messages behind" in msg and "message #2" in msg and "message #5" in msg
    assert "1 message behind" in render_agent_message("f", "diff", 2, latest_turn_n=3)
    assert "behind:" not in render_agent_message("f", "diff", 5, latest_turn_n=5)


def test_the_agent_is_told_it_can_use_only_the_ideas():
    from hindsight.delivery import render_agent_message

    for tools in (True, False):
        msg = render_agent_message("facts", "diff", 2, has_tools=tools)
        assert "not a patch you have to apply" in msg and "only its ideas" in msg


def test_the_engineers_words_are_labelled_unverified():
    from hindsight.delivery import render_agent_message

    msg = render_agent_message("facts", "diff", 2, description="it splits the store")
    assert "not verified" in msg and "it splits the store" in msg
    assert "not verified" not in render_agent_message("facts", "diff", 2)


def test_a_long_diff_says_where_the_rest_is():
    from hindsight.delivery import render_agent_message

    long = "x" * 50
    file = render_agent_message("f", long, 2, max_diff_chars=10, has_tools=False,
                                full_diff_path="/work/hindsight/d.diff")
    assert "/work/hindsight/d.diff" in file
    tool = render_agent_message("f", long, 2, max_diff_chars=10, proposal_id="H-1")
    assert "hindsight_show has the rest" in tool and "id is H-1" in tool
    short = render_agent_message("f", "x", 2, proposal_id="H-1")
    assert "id is H-1" in short  # the agent needs it for its tools, whatever the length


def test_description_holds_the_summary_and_the_plan(env):
    cfg, store = env
    run_id, proposal_id = seed_ready_run(store)
    store.set_run_chairman(run_id, {"directive": "combine A and B"})
    text = store.design_description(proposal_id)
    assert "combine A and B" in text
    assert store.design_description("missing") == ""
