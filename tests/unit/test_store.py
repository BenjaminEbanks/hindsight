"""Event store tests: state machine and cost accounting."""

from __future__ import annotations

import pytest

from hindsight.store import InvalidTransition, Store


@pytest.fixture()
def store(tmp_path):
    s = Store(tmp_path / "events.sqlite3")
    yield s
    s.close()


def test_exploration_happy_path(store):
    ex = store.create_exploration("s1", "clean_rewrite", "pi", "t1", 1, {})
    assert store.exploration_state(ex) == "PENDING"
    store.transition_exploration(ex, "PROVISIONING", "submitted")
    store.transition_exploration(ex, "RUNNING", "driver started")
    store.transition_exploration(ex, "TESTING", "driver finished")
    store.transition_exploration(ex, "GATING", "proposal saved")
    store.transition_exploration(ex, "INBOX", "passed stage 0")
    assert store.exploration_state(ex) == "INBOX"


def test_illegal_transition_rejected(store):
    ex = store.create_exploration("s1", "refactor", "pi", "t1", 1, {})
    with pytest.raises(InvalidTransition):
        store.transition_exploration(ex, "INBOX", "skip stages")
    with pytest.raises(InvalidTransition):
        store.transition_exploration(ex, "ADOPTED", "never shown")
    assert store.exploration_state(ex) == "PENDING"


def test_superseded_from_any_nonterminal(store):
    for to in ("PROVISIONING", "RUNNING"):
        ex = store.create_exploration("s1", "clean_rewrite", "pi", "t1", 1, {})
        if to == "PROVISIONING":
            store.transition_exploration(ex, "PROVISIONING", "x")
        else:
            store.transition_exploration(ex, "PROVISIONING", "x")
            store.transition_exploration(ex, "RUNNING", "x")
        store.transition_exploration(ex, "SUPERSEDED", "rebase impossible")
        assert store.exploration_state(ex) == "SUPERSEDED"


def test_terminal_states_record_finished_at(store):
    ex = store.create_exploration("s1", "x", "pi", "t1", 1, {})
    store.transition_exploration(ex, "PROVISIONING", "x")
    store.transition_exploration(ex, "FAILED", "box died")
    row = store.get_exploration(ex)
    assert row["finished_at"] is not None


def test_shown_to_inbox_and_adopted(store):
    ex = store.create_exploration("s1", "x", "pi", "t1", 1, {})
    for to in ("PROVISIONING", "RUNNING", "TESTING", "GATING", "NUDGE_READY", "SHOWN"):
        store.transition_exploration(ex, to, "step")
    store.transition_exploration(ex, "DISMISSED", "user said wrong")
    assert store.exploration_state(ex) == "DISMISSED"


def test_costs_and_spend(store):
    from hindsight.store import CostEntry

    store.record_cost(CostEntry(ref="ex-1", kind="box", box_seconds=600, usd=0.5))
    store.record_cost(CostEntry(ref="ex-2", kind="model", model="gpt-5",
                                input_tokens=1_000_000, output_tokens=100_000, usd=2.25))
    assert store.spend_since(0) == pytest.approx(2.75)
    assert store.spend_by_ref("ex-1") == pytest.approx(0.5)


def test_kv_set_semantics(store):
    assert store.kv_add_to_set("seen", "a") is True
    assert store.kv_add_to_set("seen", "a") is False
    assert store.kv_add_to_set("seen", "b") is True


def test_turns_columns_migrated_from_v1(tmp_path):
    """A v1 database (turns without usable/usable_reason/snapshot_files)
    must open cleanly and gain the new columns."""
    import sqlite3

    db = tmp_path / "old.sqlite3"
    conn = sqlite3.connect(str(db))
    conn.execute(
        "CREATE TABLE turns (id TEXT PRIMARY KEY, session_id TEXT, n INTEGER, "
        "user_message_id TEXT, status TEXT, pre_snapshot TEXT, post_snapshot TEXT, "
        "completed_at REAL)")
    conn.execute("INSERT INTO turns (id, session_id, n, status, post_snapshot) "
                 "VALUES ('t1','s1',1,'ok','snap123')")
    conn.commit()
    conn.close()

    s = Store(db)
    cols = {r["name"] for r in s._query("PRAGMA table_info(turns)")}
    assert {"usable", "usable_reason", "snapshot_files"} <= cols
    s.mark_turn_usable("t1", True, "settled")
    assert s.latest_usable_snapshot("s1")["id"] == "t1"
    s.close()
