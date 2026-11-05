"""Reaper ownership boundary .

An orchestrator must reap ONLY boxes its own store remembers. The old
implementation swept every hs-* box on the account minus its live ids —
one leftover `hindsight up` spent an evening terminating swarm probe
boxes, and a 12-wide swarm would have had episode boxes killing each
other's fleets (episode boxes are hs-* too)."""

from __future__ import annotations

from pathlib import Path

from hindsight.reaper import Reaper
from hindsight.store import Store


class FakeSail:
    def __init__(self):
        self.terminated: list[str] = []

    def terminate(self, box_id: str) -> None:
        self.terminated.append(box_id)


def _exploration(store: Store, box_id: str | None) -> str:
    ex_id = store.create_exploration(
        session_id="s", strategy="refactor", harness="pi",
        base_turn_id="turn-x", ledger_version=1, budget={})
    if box_id is not None:
        store.set_box_id(ex_id, box_id)
    return ex_id


def test_reaps_own_finished_boxes_only(tmp_path: Path):
    store = Store(tmp_path / "events.sqlite3")
    live = _exploration(store, "sb-live")
    dead = _exploration(store, "sb-dead")
    store.transition_exploration(dead, "FAILED", "done")
    sail = FakeSail()
    assert Reaper(sail, store).reap_once() == ["sb-dead"]
    assert sail.terminated == ["sb-dead"]
    assert store.exploration_state(live) != "FAILED"


def test_never_touches_boxes_outside_its_store(tmp_path: Path):
    """The 41297 regression: a stray orchestrator must not terminate boxes
    belonging to another instance (its store simply doesn't know them)."""
    store = Store(tmp_path / "events.sqlite3")
    _exploration(store, "sb-live")
    sail = FakeSail()
    assert Reaper(sail, store).reap_once() == []
    assert sail.terminated == []
    # Even with zero explorations of its own, an account full of foreign
    # hs-* boxes is none of this instance's business.
    store2 = Store(tmp_path / "other.sqlite3")
    assert Reaper(FakeSail(), store2).reap_once() == []


def test_sweeps_are_quiet_and_idempotent(tmp_path: Path):
    store = Store(tmp_path / "events.sqlite3")
    dead = _exploration(store, "sb-dead")
    store.transition_exploration(dead, "FAILED", "done")
    sail = FakeSail()
    r = Reaper(sail, store)
    assert r.reap_once() == ["sb-dead"]
    assert r.reap_once() == []  # remembered, not re-terminated
    assert sail.terminated == ["sb-dead"]


def test_terminate_failure_still_marks_reaped(tmp_path: Path):
    """Box already gone server-side: terminate raises, id is remembered so
    the store's history does not cause a noisy terminate every 5 minutes."""

    class FlakySail(FakeSail):
        def terminate(self, box_id: str) -> None:
            raise RuntimeError("box not found")

    store = Store(tmp_path / "events.sqlite3")
    dead = _exploration(store, "sb-dead")
    store.transition_exploration(dead, "FAILED", "done")
    r = Reaper(FlakySail(), store)
    assert r.reap_once() == []
    assert r.reap_once() == []
