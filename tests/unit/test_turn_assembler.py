"""Turn assembler tests: idle dedup and event mapping."""

from hindsight.sources.base import SessionEvent, TurnAssembler


def _event(kind, sid="s1", **kw):
    return SessionEvent(kind=kind, session_id=sid, ts=1.0, **kw)


def test_user_message_maps_to_pre_turn():
    d = TurnAssembler().ingest(_event("user_message", turn_id="m1"))
    assert d.action == "pre_turn"


def test_duplicate_idle_ignored():
    a = TurnAssembler()
    e = _event("turn_completed", message_ids=["u1", "a1"], messages=[])
    assert a.ingest(e).action == "process_turn"
    dup = _event("turn_completed", message_ids=["u1", "a1"], messages=[])
    assert a.ingest(dup).action == "duplicate"


def test_idle_after_retry_with_new_message_processed():
    a = TurnAssembler()
    a.ingest(_event("turn_completed", message_ids=["u1", "a1"]))
    # retry/abort replays same ids
    assert a.ingest(_event("turn_completed", message_ids=["u1", "a1"])).action == "duplicate"
    # next turn has new ids
    assert a.ingest(_event("turn_completed", message_ids=["u2", "a2"])).action == "process_turn"


def test_revert_and_error_mapping():
    a = TurnAssembler()
    assert a.ingest(_event("message_removed")).action == "revert_turn"
    assert a.ingest(_event("session_error")).action == "error_turn"
    assert a.ingest(_event("files_edited", paths=["a.py"])).action == "churn"
    assert a.ingest(_event("session_compacted")).action == "log"
