"""End-to-end orchestrator tests with a FakeSailClient and a scripted ledger
model. Unit-test mock, clearly labeled (Section 0: mocks only in unit tests,
never in eval outputs).
"""

from __future__ import annotations

import itertools
import json
import threading
import time

import pytest

from hindsight.config import HindsightConfig
from hindsight.sail_client import BoxHandle, ExecOut


from hindsight.trace import build_timeline, render_timeline


class FakeSailClient:
    """UNIT TEST MOCK: mimics the Sail surface the orchestrator uses."""

    def __init__(self):
        self.boxes: dict[str, dict] = {}
        self.n = 0
        self.written_files: dict[str, str] = {}  # survives termination
        self.checkpoint_log: list[tuple[str, int, str]] = []
        self.slept: list[str] = []

    def sleep(self, box_id):
        self.slept.append(box_id)

    def wake(self, box_id):
        pass

    def create_box(self, name, *, image=None, from_checkpoint_id=None,
                   max_lifetime_seconds=None, size=None):
        self.n += 1
        bid = f"box-{self.n}"
        self.boxes[bid] = {"name": name, "files": {}, "exists": set()}
        return BoxHandle(id=bid, name=name)

    def run(self, box_id, command, *, timeout=None, cwd=None, env=None):
        return ExecOut(0, "fakebaselinecommit123\n", "", False)

    def start_background(self, box_id, command, *, cwd=None, env=None):
        pass

    def write_files(self, box_id, files):
        self.boxes[box_id]["files"].update(files)
        self.written_files.update(files)

    def read_file(self, box_id, path):
        written = self.boxes.get(box_id, {}).get("files", {})
        if path == "/work/task.json" and path in written:
            return written[path].encode()
        if path.endswith("patch.diff"):
            # selection qualification requires product-code
            # changes — a tests-only (or empty) patch is the signature of
            # a hollow worker and never qualifies.
            return ("diff --git a/counter.py b/counter.py\n"
                    "index 8302f2a..5a5bbf6 100644\n"
                    "--- a/counter.py\n"
                    "+++ b/counter.py\n"
                    "@@ -1,2 +1,2 @@\n"
                    "-old = 1\n"
                    "+new = 2\n").encode()
        if path.endswith("result.json"):
            return json.dumps({
                "exploration_id": "ex-1", "harness": "pi", "exit_reason": "done",
                "agent_turns": 5, "input_tokens": 100, "output_tokens": 200,
                "tests": {"passed": 3, "failed": 0, "names": []},
                "diff_stat": {"files": 1, "added": 10, "removed": 2},
                "summary": {"summary": "moved counts to a shared store"},
            }).encode()
        if path.endswith("status.json"):
            return json.dumps({"phase": "finished", "heartbeat": 1.0}).encode()
        if path.endswith("proj_junit.xml"):
            return (b'<?xml version="1.0"?><testsuite tests="1" failures="0">'
                    b'<testcase name="test_app"></testcase></testsuite>')
        return b""

    def upload_file(self, box_id, local_path, guest_path):
        pass

    def download_file(self, box_id, guest_path, local_path):
        pass

    def file_exists(self, box_id, path):
        return path in self.boxes[box_id]["exists"] or path.endswith("result.json")

    def list_dir(self, box_id, path):
        return []

    def checkpoint(self, box_id, ttl_seconds=None, name=None):
        # Resume-checkpoint log: (box, ttl, name) tuples.
        self.checkpoint_log.append((box_id, ttl_seconds or 0, name or ""))
        return f"cp-{len(self.checkpoint_log)}"

    def terminate(self, box_id):
        self.boxes.pop(box_id, None)

    def list_boxes(self, search=None):
        return []

    def inference_chat(self, model, messages, *, extra_headers=None, **payload):
        return {"choices": [{"message": {"content": "unused"}}], "usage": {}}


def scripted_model_fn(patch_json: str):
    def fn(prompt: str) -> str:
        return patch_json

    return fn


NEW_REQUIREMENT_TEXT = "this has to work across three servers"


@pytest.fixture()
def project(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "app.py").write_text("x = 1\n")
    (repo / "test_app.py").write_text("def test_x():\n    assert x == 1\n")
    cfg = HindsightConfig(project_dir=repo, hindsight_home=tmp_path / "hs")
    cfg.explore.poll_interval_seconds = 0.05  # fast monitors in tests
    return cfg


def _run_turn(orch, session_id="s1", text=NEW_REQUIREMENT_TEXT, n=1):
    from hindsight.sources.base import SessionEvent

    orch.handle_event(SessionEvent(kind="user_message", session_id=session_id,
                                  turn_id=f"m{n}", ts=1.0))
    orch.handle_event(SessionEvent(
        kind="turn_completed", session_id=session_id, turn_id=f"m{n}",
        message_ids=[f"m{n}", f"a{n}"],
        messages=[{"id": f"m{n}", "role": "user", "text": text},
                  {"id": f"a{n}", "role": "assistant", "text": "done"}],
        ts=2.0))
    assert orch.flush()


def test_shutdown_waits_for_inflight_timer_callback(project):
    """Regression (: a straggler idle timer fired after
    shutdown() had closed the store -> sqlite3.ProgrammingError "Cannot
    operate on a closed database" from a late cost write.
    Timer.cancel() only stops timers that have not started, so shutdown
    must (a) skip not-yet-started callbacks and (b) wait for an in-flight
    one before closing the store."""
    from hindsight.orchestrator import Orchestrator

    orch = Orchestrator(project,
                        model_fn=scripted_model_fn(json.dumps({"ops": []})),
                        sail=FakeSailClient())
    started, done, never = threading.Event(), threading.Event(), threading.Event()

    def slow_callback():
        started.set()
        time.sleep(0.5)  # a mid-flight callback doing model calls
        # The probe3 crash was exactly this: a store write after the model
        # call, against a store shutdown() had already closed.
        orch.store.upsert_session("s1", "p", "opencode", "unknown")
        done.set()

    def late_callback():
        never.set()

    orch._schedule_settle("t-now", 0.01, slow_callback)   # will be mid-flight
    orch._schedule_settle("t-late", 30.0, late_callback)  # must never run
    assert started.wait(5)
    orch.shutdown()
    assert done.is_set()        # shutdown waited for the in-flight callback
    assert not never.is_set()   # pending timer was canceled, not run


def _wait_for_run(orch, timeout=15):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if orch.store._query(
                "SELECT 1 FROM runs WHERE state NOT IN "
                "('NEW', 'PLANNED', 'LAUNCHED', 'SELECTING')"):
            return
        time.sleep(0.1)


def _new_requirement_on_turn_2():
    def fn(prompt: str) -> str:
        if "Name between 2 and 5 directions" in prompt:
            return json.dumps({"directions": [
                {"name": f"d{i}", "structure": f"structure {i}", "why": "r"}
                for i in range(5)]})
        return json.dumps({"call_background": NEW_REQUIREMENT_TEXT in prompt,
                           "what_no_longer_holds": "shared store"})

    return fn


def test_no_requirement_change_starts_nothing(project):
    from hindsight.orchestrator import Orchestrator

    project.experiments.settle_seconds = 0.05
    orch = Orchestrator(project, model_fn=scripted_model_fn(json.dumps({"ops": []})),
                        sail=FakeSailClient())
    _run_turn(orch, text="add a readme")
    time.sleep(0.6)
    assert orch.store.latest_session_check("s1")["priority"] == "low"
    assert orch.store._query("SELECT * FROM explorations") == []
    orch.shutdown()


def test_changed_requirement_runs_the_whole_flow(project):
    """User message -> requirement change noticed -> workers check the code
    against the verbatim messages -> one result, ready to send."""
    from hindsight.orchestrator import Orchestrator

    project.experiments.settle_seconds = 0.05
    fake_sail = FakeSailClient()
    orch = Orchestrator(project, model_fn=_new_requirement_on_turn_2(),
                        judge_model_fn=lambda p: "FINAL RANKING:\n1. Proposal A\n\n###DIRECTIVE\nBuild it.", sail=fake_sail)
    _run_turn(orch, text="build the app", n=1)
    _run_turn(orch, n=2)
    _wait_for_run(orch)

    runs = orch.store._query("SELECT * FROM runs")
    assert len(runs) == 1
    assert json.loads(runs[0]["target_requirement_ids_json"]) == ["T2"]
    assert runs[0]["n_workers"] == 6
    assert runs[0]["state"] in ("READY_TO_DELIVER", "DELIVERED"), runs[0]["exit_reason"]
    assert runs[0]["winner_proposal_id"]
    # What workers were told is the user's words, not the ledger's.
    task = json.loads(fake_sail.written_files["/work/task.json"])
    assert "build the app" in task["prompt"] and NEW_REQUIREMENT_TEXT in task["prompt"]
    assert "work across three servers" not in task["prompt"].replace(
        NEW_REQUIREMENT_TEXT, "")
    # The final worker's design rests; all 6 workers' boxes are gone and its own
    # survives.
    exs = orch.store._query("SELECT * FROM explorations")
    assert sum(e["state"] in ("NUDGE_READY", "SHOWN") for e in exs) == 1
    assert sum(e["state"] == "DISCARDED" for e in exs) == 6
    winner = next(e for e in exs if e["state"] in ("NUDGE_READY", "SHOWN"))
    assert list(fake_sail.boxes) == [winner["box_id"]]
    stages = orch.store._query(
        "SELECT stage FROM gate_results WHERE proposal_id=?",
        (runs[0]["winner_proposal_id"],))
    assert {"0_integrity", "2_audit_gate"} <= {s["stage"] for s in stages}
    # The trace can explain all of it: every model step was kept, under this session.
    calls = orch.store.model_calls(runs[0]["session_id"])
    assert {"design_review", "council_critique", "council_chairman"} <= {
        c["step"] for c in calls}
    assert all(not c["error"] for c in calls)
    timeline = render_timeline(build_timeline(orch.store, runs[0]["session_id"]))
    for expected in ("START RUN", "2_audit_gate pass", "READY_TO_DELIVER"):
        assert expected in timeline
    orch.shutdown()


def test_turn_timer_firing_during_shutdown_is_harmless(project):
    from hindsight.orchestrator import Orchestrator

    orch = Orchestrator(project, model_fn=_new_requirement_on_turn_2(),
                        sail=FakeSailClient())
    _run_turn(orch, text="build the app", n=1)
    _run_turn(orch, text=NEW_REQUIREMENT_TEXT, n=2)
    orch._reviews.shutdown(wait=True)

    orch._detect("s1")
    orch.shutdown()


def test_explore_now_starts_a_run_and_respects_one_run_per_session(project):
    from hindsight.orchestrator import Orchestrator

    project.experiments.settle_seconds = 0.05
    orch = Orchestrator(project, model_fn=_new_requirement_on_turn_2(),
                        judge_model_fn=lambda p: "FINAL RANKING:\n1. Proposal A\n\n###DIRECTIVE\nBuild it.", sail=FakeSailClient())
    _run_turn(orch, text="build the app", n=1)
    time.sleep(0.3)
    ids = orch.explore_now("s1", ["open_redesign"])
    assert len(ids) == 1
    assert orch.explore_now("s1") == []
    orch.shutdown()


def test_session_end_lets_the_kept_box_go(project):
    from hindsight.orchestrator import Orchestrator
    from hindsight.sources.base import SessionEvent

    fake_sail = FakeSailClient()
    orch = Orchestrator(project, model_fn=scripted_model_fn(json.dumps({"ops": []})),
                        sail=fake_sail)
    box = fake_sail.create_box("hs-x")
    ex_id = orch.store.create_exploration("s1", "refactor", "opencode", "t", 1, {})
    orch.store.set_box_id(ex_id, box.id)
    orch.resume.keep(ex_id)
    orch.handle_event(SessionEvent(kind="session_ended", session_id="s1", ts=1.0))
    assert orch.flush()
    assert box.id not in fake_sail.boxes
    assert orch.store.kept_box("s1") is None
    orch.shutdown()


def test_settle_guard_marks_snapshot_usable(project):
    from hindsight.orchestrator import Orchestrator

    project.experiments.settle_seconds = 0.05
    orch = Orchestrator(project, model_fn=scripted_model_fn(json.dumps({"ops": []})),
                        sail=FakeSailClient())
    _run_turn(orch, text="add a readme")
    time.sleep(0.3)
    row = orch.store.latest_turn("s1")
    assert row["usable"] == 1
    assert row["usable_reason"] == "settled"
    orch.shutdown()


def test_settle_guard_files_changing_marks_unusable(project):
    from hindsight.orchestrator import Orchestrator

    project.experiments.settle_seconds = 0.05
    orch = Orchestrator(project, model_fn=scripted_model_fn(json.dumps({"ops": []})),
                        sail=FakeSailClient())

    from hindsight.sources.base import SessionEvent

    orch.handle_event(SessionEvent(kind="user_message", session_id="s1",
                                  turn_id="m1", ts=1.0))
    orch.handle_event(SessionEvent(
        kind="turn_completed", session_id="s1", turn_id="m1",
        message_ids=["m1", "a1"],
        messages=[{"id": "m1", "role": "user", "text": "hi"},
                  {"id": "a1", "role": "assistant", "text": "ok"}], ts=2.0))
    assert orch.flush()
    # keep changing a file through both settle checks
    for i in range(12):
        (project.project_dir / "app.py").write_text(f"x = {i}\n")
        time.sleep(0.05)
    time.sleep(0.3)
    row = orch.store.latest_turn("s1")
    assert row["usable"] == 0
    assert row["usable_reason"] == "files still changing"
    orch.shutdown()


def test_handle_event_returns_fast(project):
    """The plugin's send budget is 200ms: handle_event must queue, not block,
    even when the pipeline does slow work (model calls)."""
    from hindsight.orchestrator import Orchestrator
    from hindsight.sources.base import SessionEvent

    def slow_model(prompt: str) -> str:
        time.sleep(1.0)
        return json.dumps({"ops": []})

    orch = Orchestrator(project, model_fn=slow_model, sail=FakeSailClient())
    t0 = time.time()
    orch.handle_event(SessionEvent(kind="user_message", session_id="s1",
                                   turn_id="m1", ts=1.0))
    orch.handle_event(SessionEvent(
        kind="turn_completed", session_id="s1", turn_id="m1",
        message_ids=["m1", "a1"],
        messages=[{"id": "m1", "role": "user", "text": "hello"},
                  {"id": "a1", "role": "assistant", "text": "hi"}], ts=2.0))
    elapsed = time.time() - t0
    assert elapsed < 0.5, f"handle_event blocked for {elapsed:.2f}s"
    assert orch.flush(timeout=10)
    orch.shutdown()


def test_post_snapshot_failure_marks_turn_errored_not_stuck(project):
    """Regression : when the post-turn snapshot raised,
    the turn row was left in_progress (post=None) and the NEXT turn's events
    attached to that dead row — turn 3 was never recorded at all. A failed
    post snapshot must mark the turn snapshot_error, and later turns must
    still get their own rows."""
    from hindsight.orchestrator import Orchestrator
    from hindsight.snapshot import SnapshotError

    orch = Orchestrator(project,
                       model_fn=scripted_model_fn(json.dumps({"ops": []})),
                       sail=FakeSailClient())
    from hindsight.sources.base import SessionEvent

    def run_turn(n: int) -> None:
        # unique turn ids: the assembler dedupes on them (a reused id
        # silently drops the second turn_completed)
        mid = f"m{n}"
        orch.handle_event(SessionEvent(kind="user_message", session_id="s1",
                                        turn_id=mid, ts=1.0))
        orch.handle_event(SessionEvent(
            kind="turn_completed", session_id="s1", turn_id=mid,
            message_ids=[mid, f"a{n}"],
            messages=[{"id": mid, "role": "user", "text": "stage"},
                      {"id": f"a{n}", "role": "assistant", "text": "done"}],
            ts=2.0))
        assert orch.flush()

    state = {"fail_post": False}
    real_snapshot = orch.snapshotter.snapshot

    def flaky_snapshot(session_id, turn_n, phase):
        if phase == "post" and state["fail_post"]:
            raise SnapshotError("simulated git failure")
        return real_snapshot(session_id, turn_n, phase)

    orch.snapshotter.snapshot = flaky_snapshot
    try:
        run_turn(1)  # turn 1: clean
        rows = orch.store._query("SELECT n, status FROM turns ORDER BY n")
        assert [r["n"] for r in rows] == [1]
        assert rows[0]["status"] == "ok"

        state["fail_post"] = True
        run_turn(2)  # turn 2: post snapshot dies
        rows = orch.store._query("SELECT n, status FROM turns ORDER BY n")
        assert [r["n"] for r in rows] == [1, 2]
        assert rows[1]["status"] == "snapshot_error"

        state["fail_post"] = False
        run_turn(3)  # turn 3: own row, no glomming onto turn 2
        rows = orch.store._query("SELECT n, status FROM turns ORDER BY n")
        assert [r["n"] for r in rows] == [1, 2, 3]
        assert rows[2]["status"] == "ok"
    finally:
        orch.shutdown()


def test_snapshot_error_turn_gets_usable_verdict(project):
    """Regression: the snapshot_error
    path completed the turn but never recorded a settle verdict, leaving
    usable=NULL forever — a waiter on the settle chain (eval episodes)
    would hang until quiet timeout."""
    from hindsight.orchestrator import Orchestrator
    from hindsight.snapshot import SnapshotError

    project.experiments.settle_seconds = 0.05
    orch = Orchestrator(project,
                        model_fn=scripted_model_fn(json.dumps({"ops": []})),
                        sail=FakeSailClient())
    real_snapshot = orch.snapshotter.snapshot

    def failing_snapshot(session_id, turn_n, phase):
        if phase == "post":
            raise SnapshotError("simulated git failure")
        return real_snapshot(session_id, turn_n, phase)

    orch.snapshotter.snapshot = failing_snapshot
    _run_turn(orch)
    deadline = time.time() + 10
    turn = None
    while time.time() < deadline:
        turn = orch.store._query_one("SELECT * FROM turns")
        if turn and turn["usable"] is not None:
            break
        time.sleep(0.05)
    assert turn is not None and turn["usable"] == 0
    assert turn["usable_reason"] == "post-turn snapshot failed"
    orch.shutdown()


def test_settle_unusable_still_runs_detection(project):
    """An unusable settle verdict must still leave a detection decision."""
    from hindsight.orchestrator import Orchestrator

    project.experiments.settle_seconds = 0.05
    orch = Orchestrator(project,
                        model_fn=scripted_model_fn(json.dumps({"ops": []})),
                        sail=FakeSailClient())
    calls = itertools.count()
    # A fingerprint that changes on every call: "files still changing".
    orch.snapshotter.work_tree_fingerprint = lambda: f"f{next(calls)}"
    _run_turn(orch)
    deadline = time.time() + 10
    turn = None
    check = None
    while time.time() < deadline:
        turn = orch.store._query_one("SELECT * FROM turns")
        check = orch.store.latest_session_check("s1")
        if turn and turn["usable"] == 0 and check is not None:
            break
        time.sleep(0.05)
    assert turn is not None and turn["usable"] == 0
    assert turn["usable_reason"] == "files still changing"
    assert check is not None
    assert check["turn_id"] == turn["id"]
    orch.shutdown()


def test_detection_needs_no_quiet_time(project):
    from hindsight.orchestrator import Orchestrator

    project.experiments.settle_seconds = 0.01
    orch = Orchestrator(project, model_fn=scripted_model_fn(json.dumps({"ops": []})),
                        sail=FakeSailClient())
    _run_turn(orch)
    deadline = time.time() + 5
    while time.time() < deadline and orch.store.latest_session_check("s1") is None:
        time.sleep(0.02)
    assert orch.store.latest_session_check("s1") is not None
    orch.shutdown()


def test_delivery_is_not_stored_as_something_the_user_said(project):
    from hindsight.delivery import DELIVERY_MARKER
    from hindsight.orchestrator import Orchestrator
    from hindsight.sources.base import SessionEvent

    project.experiments.settle_seconds = 0.01
    orch = Orchestrator(project, model_fn=scripted_model_fn(json.dumps({"ops": []})),
                        sail=FakeSailClient())
    orch.handle_event(SessionEvent(kind="user_message", session_id="s1",
                                  turn_id="m1", ts=1.0))
    orch.handle_event(SessionEvent(
        kind="turn_completed", session_id="s1", turn_id="m1", message_ids=["d", "m1"],
        messages=[{"id": "d", "role": "user", "text": DELIVERY_MARKER + " a design"},
                  {"id": "m1", "role": "user", "text": "real request"}], ts=2.0))
    assert orch.flush()
    texts = [m["text"] for m in orch.store.messages_for_turn(
        orch.store.latest_turn("s1")["id"])]
    assert texts == ["real request"]
    orch.shutdown()


def test_designs_are_not_released_with_events_still_queued(project):
    from hindsight.orchestrator import Orchestrator

    orch = Orchestrator(project, model_fn=scripted_model_fn(json.dumps({"ops": []})),
                        sail=FakeSailClient())
    calls = []
    orch.delivery.maybe_deliver = lambda sid: calls.append(sid)
    with orch._idle:
        orch._processing = True
    assert orch.designs_for_agent("s1") == [] and calls == []
    with orch._idle:
        orch._processing = False
    assert orch.designs_for_agent("s1") == [] and calls == ["s1"]
    orch.shutdown()


def test_failed_review_starts_nothing_and_still_records_a_decision(project):
    """Live hang (paced1): a timed-out model call crashed the turn handler, so
    the turn never settled and detection never ran."""
    from hindsight.orchestrator import Orchestrator

    project.experiments.settle_seconds = 0.05

    def fail(prompt):
        raise TimeoutError("inference timed out")

    orch = Orchestrator(project, model_fn=lambda p: "note", reviewer_model_fn=fail,
                        sail=FakeSailClient())
    _run_turn(orch, text="build the app", n=1)
    _run_turn(orch, text="now something else", n=2)
    deadline = time.time() + 5
    check = None
    while time.time() < deadline:
        check = orch.store.latest_session_check("s1")
        turn = orch.store.latest_turn("s1")
        if check is not None and check["turn_id"] == turn["id"]:
            break
        time.sleep(0.02)
    assert check["turn_id"] == orch.store.latest_turn("s1")["id"]
    assert check["priority"] == "low"
    assert orch.store._query("SELECT * FROM runs") == []
    orch.shutdown()


def test_slow_review_does_not_hold_up_the_next_turn(project):
    """The pre-turn snapshot must land before the agent edits anything."""
    from hindsight.orchestrator import Orchestrator

    project.experiments.settle_seconds = 0.01
    started, release = threading.Event(), threading.Event()

    def slow(prompt):
        started.set()
        release.wait(10)
        return json.dumps({"call_background": False})

    orch = Orchestrator(project, model_fn=lambda p: "note", reviewer_model_fn=slow,
                        sail=FakeSailClient())
    _run_turn(orch, text="build the app", n=1)
    _run_turn(orch, text="now something else", n=2)
    assert started.wait(5)
    t0 = time.time()
    _run_turn(orch, text="and a third", n=3)
    assert orch.flush(timeout=5)
    assert time.time() - t0 < 5
    assert orch.store.latest_turn("s1")["n"] == 3
    release.set()
    orch.shutdown()


def test_open_review_counts_until_its_verdict_is_in():
    """A slow verdict is recorded against the latest turn, so the episode cannot
    tell from checks alone. It once shut down with the last review still running."""
    import threading
    from types import SimpleNamespace

    from hindsight.orchestrator import Orchestrator

    orch = Orchestrator.__new__(Orchestrator)
    orch._reviews_lock = threading.Lock()
    orch._reviews_open = 1
    orch.store = None
    orch.cfg = SimpleNamespace(models=SimpleNamespace(reviewer="m"))
    orch._reviewer_model_fn = None
    orch._guarded = lambda fn, args: (_ for _ in ()).throw(RuntimeError("boom"))
    import hindsight.orchestrator as mod
    real = mod.design_review.review
    mod.design_review.review = lambda prompt, fn: None
    real_recorded = mod.recorded
    mod.recorded = lambda *a, **k: None
    try:
        assert orch.reviews_open() == 1
        try:
            orch._review("s", "p")
        except RuntimeError:
            pass
        assert orch.reviews_open() == 0
    finally:
        mod.design_review.review = real
        mod.recorded = real_recorded
