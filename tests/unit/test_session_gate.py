"""Session gate tests."""

from __future__ import annotations

import pytest

from hindsight.config import ExperimentsConfig, HindsightConfig
from hindsight.design_review import Verdict
from hindsight.session_gate import evaluate_gate
from hindsight.store import Store


@pytest.fixture()
def store(tmp_path):
    s = Store(tmp_path / "db.sqlite3")
    yield s
    s.close()


def _add_turns(store, session_id: str, n_turns: int, files: int = 3,
              statuses: list[str] | None = None):
    statuses = statuses or ["ok"] * n_turns
    for i in range(n_turns):
        turn_id = store.start_turn(session_id, i + 1, f"m{i}", f"pre{i}")
        store.complete_turn(turn_id, statuses[i], f"post{i}", snapshot_files=files)
        store.mark_turn_usable(turn_id, statuses[i] == "ok", "settled" if statuses[i] == "ok" else statuses[i])


def _cfg(**kw) -> HindsightConfig:
    return HindsightConfig(hindsight_home=None, **kw)


def _cfg_exp(**exp_kw) -> HindsightConfig:
    return _cfg(experiments=ExperimentsConfig(**exp_kw))


def test_worthy_after_one_turn_with_no_waiting(store):
    _add_turns(store, "s1", 1)
    cfg = _cfg()
    r = evaluate_gate(store, cfg, "s1", None)
    assert r.worthy
    assert r.priority == "low"


def test_no_usable_snapshot_not_worthy(store):
    _add_turns(store, "s1", 8)
    store._exec("UPDATE turns SET usable=0")
    cfg = _cfg()
    r = evaluate_gate(store, cfg, "s1", None)
    assert not r.worthy
    assert any("runnable" in x for x in r.reasons)


def test_review_yes_raises_priority(store):
    _add_turns(store, "s1", 2)
    r = evaluate_gate(store, _cfg(), "s1", Verdict(True, "shared store from the start"))
    assert r.worthy
    assert r.priority == "raised"
    assert any("shared store" in x for x in r.reasons)


def test_review_no_or_failed_starts_nothing(store):
    _add_turns(store, "s1", 2)
    assert evaluate_gate(store, _cfg(), "s1", Verdict(False, "no")).priority == "low"
    assert evaluate_gate(store, _cfg(), "s1", None).priority == "low"


def test_active_run_blocks_the_gate_for_that_session_only(store):
    _add_turns(store, "s1", 8)
    _add_turns(store, "s2", 8)
    store.create_run("s1", 1, ["R1"])
    busy = evaluate_gate(store, _cfg(), "s1", None)
    assert not busy.worthy
    assert not busy.checks["no_active_run"]["passed"]
    assert evaluate_gate(store, _cfg(), "s2", None).worthy


def test_decision_logged(store):
    _add_turns(store, "s1", 8)
    cfg = _cfg()
    evaluate_gate(store, cfg, "s1", None)
    row = store.latest_session_check("s1")
    assert row is not None
    assert row["priority"] == "low"
    assert row["worthy"] == 1


