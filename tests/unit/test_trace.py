"""The trace answers 'what happened and why' from what the store records."""

from __future__ import annotations

import json

import pytest

from hindsight.models import call_context, recorded
from hindsight.store import Store
from hindsight.trace import build_timeline, render_call, render_timeline


@pytest.fixture()
def store(tmp_path):
    s = Store(tmp_path / "db.sqlite3")
    yield s
    s.close()


def texts(store, kind):
    return [e["text"] for e in build_timeline(store, "s1") if e["kind"] == kind]


def test_empty_session_says_so(store):
    assert "nothing recorded" in render_timeline(build_timeline(store, "s1"))


def test_a_run_reads_in_order_with_its_reasons(store):
    tid = store.start_turn("s1", 1, "m1", "pre")
    store.add_messages("s1", tid, [{"id": "m1", "role": "user", "text": "build it"}])
    store.complete_turn(tid, "ok", "post")
    store.mark_turn_usable(tid, True, "settled")
    store.save_session_check("s1", tid, True, "raised", {},
                             ["design review: use a shared store"])
    run = store.create_run("s1", 0, ["T1"])
    store.transition_run(run, "PLANNED", "design review said yes")
    ex = store.create_exploration("s1", "refactor", "opencode", tid, 0, {}, run_id=run)
    for state in ("PROVISIONING", "RUNNING", "TESTING", "GATING"):
        store.transition_exploration(ex, state, "seed")
    pid = store.save_proposal(ex, "patch", {"summary": "s"}, {"files": 1})
    store.save_gate_result(pid, "1_worker_tests", True, {"passed": 3, "failed": 2}, "own run")
    store.save_gate_result(pid, "2_audit_gate", False, {}, "1 regression(s)")
    store.transition_exploration(ex, "DISCARDED", "1 regression(s)")

    events = build_timeline(store, "s1")

    assert [e["ts"] for e in events] == sorted(e["ts"] for e in events)
    out = render_timeline(events)
    assert "build it" in out
    assert "START RUN: design review: use a shared store" in out
    assert "PLANNED: design review said yes" in out
    assert "refactor 2_audit_gate FAIL: 1 regression(s)" in out
    assert "refactor DISCARDED" in out
    assert "refactor finished with 3 tests passing, 2 failing" in out


def test_another_sessions_events_stay_out(store):
    tid = store.start_turn("s2", 1, "m1", "pre")
    store.add_messages("s2", tid, [{"id": "m1", "role": "user", "text": "theirs"}])
    assert "theirs" not in render_timeline(build_timeline(store, "s1"))


def test_model_call_keeps_prompt_reply_and_version(store):
    fn = recorded(store, "m", "design_review", lambda p: json.dumps({"ok": p}))
    with call_context(session_id="s1"):
        fn("the prompt")
    [line] = texts(store, "model")
    assert line.startswith("#1 design_review (m,")
    shown = render_call(store, 1)
    assert "the prompt" in shown and '{"ok": "the prompt"}' in shown
    assert "prompt v1" in shown


def test_failed_model_call_is_recorded_and_still_raises(store):
    def boom(p):
        raise TimeoutError("inference timed out")

    with call_context(session_id="s1"), pytest.raises(TimeoutError):
        recorded(store, "m", "design_review", boom)("p")
    [line] = texts(store, "model")
    assert "ERROR TimeoutError: inference timed out" in line


def test_call_context_names_the_judge_step_and_finds_the_session_by_run(store):
    run = store.create_run("s1", 0, ["T1"])
    fn = recorded(store, "m", "council", lambda p: "ok")
    with call_context(step="council_chairman", run_id=run):
        fn("p")
    assert "#1 council_chairman" in texts(store, "model")[0]


def test_worker_transcript_round_trip(store):
    assert store.worker_transcript("x") is None
    store.save_worker_transcript("x", "line one\nline two")
    assert store.worker_transcript("x") == "line one\nline two"


def test_cli_shows_the_timeline_a_call_and_a_transcript(tmp_path):
    from typer.testing import CliRunner

    from hindsight.cli import app
    from hindsight.config import load_config

    project = tmp_path / "proj"
    project.mkdir()
    cfg = load_config(str(project))
    cfg.db_path.parent.mkdir(parents=True, exist_ok=True)
    s = Store(cfg.db_path)
    with call_context(session_id="s1"):
        recorded(s, "m", "design_review", lambda p: "the reply")("the prompt")
    s.save_worker_transcript("ex1", "worker said hi")
    s.close()

    def run(*args):
        r = CliRunner().invoke(app, ["trace", "s1", "--project-dir", str(project), *args])
        assert r.exit_code == 0, r.output
        return r.output

    assert "#1 design_review" in run()
    assert "the prompt" in run("--call", "1") and "the reply" in run("--call", "1")
    assert "worker said hi" in run("--worker", "ex1")
    assert json.loads(run("--json"))[0]["kind"] == "model"


def test_failing_to_record_never_loses_the_answer(store, monkeypatch):
    def broken(**kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(store, "save_model_call", broken)
    assert recorded(store, "m", "design_review", lambda p: "answer")("p") == "answer"


def test_a_call_with_no_reply_yet_shows_in_the_trace(tmp_path):
    import threading

    from hindsight.models import call_context, recorded
    from hindsight.store import Store
    from hindsight.trace import build_timeline

    store = Store(tmp_path / "e.sqlite3")
    started, release = threading.Event(), threading.Event()

    def slow(prompt):
        started.set()
        release.wait(5)
        return "answer"

    def call():
        with call_context(session_id="s"):
            recorded(store, "m", "design_review", slow)("p")

    t = threading.Thread(target=call)
    t.start()
    started.wait(5)
    assert any("no reply yet" in e["text"] for e in build_timeline(store, "s"))
    release.set()
    t.join()
    text = [e["text"] for e in build_timeline(store, "s")]
    assert not any("no reply yet" in x for x in text) and any(" ok" in x for x in text)
    assert store.model_call(1)["response"] == "answer"
    store.close()
