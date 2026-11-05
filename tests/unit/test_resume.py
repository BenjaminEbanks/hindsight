"""The main agent's questions for the winning worker: its sleeping box, the
checkpoint fallback, and when the box is let go.

Sail and harness dependencies are injected fakes (UNIT TEST MOCKS). The
driver's answer mode runs for real in a temp dir (it is standalone stdlib
code), so its file contract is exercised, not stubbed.
"""

from __future__ import annotations

import json
import time

import pytest

from hindsight.config import HindsightConfig
from hindsight.resume import WorkerResume
from hindsight.reaper import Reaper
from hindsight.sail_client import BoxHandle, ExecOut, SailClientError
from hindsight.store import Store


class ResumeSail:
    """UNIT TEST MOCK: checkpoint + revive + answer flow."""

    def __init__(self, *, answer_after_polls: int = 1,
                 answer: dict | None = None):
        self.checkpoints: list[tuple[str, int, str]] = []
        self.revived: list[str] = []          # from_checkpoint_id args
        self.written: dict[tuple[str, str], str] = {}
        self.started: list[str] = []
        self.terminated: list[str] = []
        self.woken: list[str] = []
        self.slept: list[str] = []
        self.wake_fails = False
        self.exists: set[tuple[str, str]] = set()
        self.polls = 0
        self.answer_after_polls = answer_after_polls
        self.answer = answer or {
            "question": "why?",
            "answer": "I moved the counter into the store.",
            "exit_reason": "done", "agent_turns": 1,
            "input_tokens": 50, "output_tokens": 80}

    def create_box(self, name, *, image=None, from_checkpoint_id=None,
                   max_lifetime_seconds=None, size=None):
        bid = f"box-{len(self.revived) + 1}"
        if from_checkpoint_id:
            self.revived.append(from_checkpoint_id)
        return BoxHandle(id=bid, name=name)

    def checkpoint(self, box_id, ttl_seconds=None, name=None):
        self.checkpoints.append((box_id, ttl_seconds or 0, name or ""))
        return f"cp-{len(self.checkpoints)}"

    def write_files(self, box_id, files):
        for path, content in files.items():
            self.written[(box_id, path)] = content

    def start_background(self, box_id, command):
        self.started.append(box_id)

    def run(self, box_id, command, *, timeout=None, cwd=None, env=None):
        return ExecOut(0, "", "", False)

    def file_exists(self, box_id, path):
        self.polls += 1
        if path == "/work/answer.json" and \
                self.polls >= self.answer_after_polls:
            self.exists.add((box_id, path))
            self.written[(box_id, path)] = json.dumps(self.answer)
        return (box_id, path) in self.exists

    def read_file(self, box_id, path):
        return self.written[(box_id, path)].encode()

    def upload_file(self, box_id, local_path, guest_path):
        pass

    def terminate(self, box_id):
        self.terminated.append(box_id)

    def wake(self, box_id):
        if self.wake_fails:
            raise SailClientError("box gone")
        self.woken.append(box_id)

    def sleep(self, box_id):
        self.slept.append(box_id)


@pytest.fixture()
def env(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "app.py").write_text("x = 1\n")
    cfg = HindsightConfig(project_dir=repo, hindsight_home=tmp_path / "hs")
    store = Store(cfg.db_path)
    yield cfg, store
    store.close()


def seed_proposal(store, *, session_id="s1", checkpoint_id="cp-9",
                  harness="opencode", box_id=None) -> str:
    """A proposal whose exploration finished with a resume checkpoint."""
    ex_id = store.create_exploration(
        session_id, "clean_rewrite", harness, "t1", 1,
        {"target_ids": ["R1"]}, run_id="")
    for to in ("PROVISIONING", "RUNNING", "TESTING", "GATING"):
        store.transition_exploration(ex_id, to, "seed")
    proposal_id = store.save_proposal(
        ex_id, "--- a/app.py\n+++ b/app.py\n",
        {"summary": "moved counts to a shared store"},
        {"files": 1, "added": 5, "removed": 1})
    if checkpoint_id:
        store.set_exploration_checkpoint(ex_id, checkpoint_id)
    if box_id:
        store.set_box_id(ex_id, box_id)
    return proposal_id


def _ex(store, proposal_id):
    return store.get_proposal(proposal_id)["exploration_id"]


def test_ask_wakes_the_sleeping_winner_and_puts_it_back(env):
    cfg, store = env
    proposal_id = seed_proposal(store, box_id="worker-box")
    sail = ResumeSail()
    resume = WorkerResume(cfg, store, sail)
    resume.keep(_ex(store, proposal_id))

    out = resume.answer(proposal_id, "why?")

    assert out["source"] == "worker_resume"
    assert sail.woken == ["worker-box"] and sail.slept == ["worker-box"]
    assert sail.revived == [] and sail.terminated == []
    task = json.loads(sail.written[("worker-box", "/work/task.json")])
    # The same agent conversation, so it remembers its work.
    assert task["agent_session"] == f"ses-hs-worker-{_ex(store, proposal_id)}"[:60]
    assert task["model"] == f"{cfg.models.provider}/{cfg.models.worker}"
    assert store.kept_box("s1")["box_id"] == "worker-box"


def test_ask_falls_back_to_the_checkpoint_when_waking_fails(env):
    cfg, store = env
    proposal_id = seed_proposal(store, box_id="worker-box")
    sail = ResumeSail()
    sail.wake_fails = True
    resume = WorkerResume(cfg, store, sail)
    resume.keep(_ex(store, proposal_id))

    out = resume.answer(proposal_id, "why?")

    assert out["source"] == "worker_resume"
    assert sail.revived == ["cp-9"]
    assert store.kept_box("s1") is None


def test_ask_that_times_out_does_not_leave_the_box_awake(env):
    cfg, store = env
    proposal_id = seed_proposal(store, box_id="worker-box")
    sail = ResumeSail(answer_after_polls=10_000)
    resume = WorkerResume(cfg, store, sail)
    resume.keep(_ex(store, proposal_id))

    assert resume.answer(proposal_id, "why?", timeout=0.2)["source"] == "timeout"
    assert sail.terminated == ["worker-box"]
    assert store.kept_box("s1") is None


def test_ask_about_any_proposal_of_a_run_goes_to_its_winner(env):
    cfg, store = env
    run_id = store.create_run("s1", 1, ["R1"])
    loser = seed_proposal(store, box_id="loser-box")
    winner = seed_proposal(store, box_id="winner-box")
    for pid in (loser, winner):
        store._exec("UPDATE explorations SET run_id=? WHERE id=?",
                    (run_id, _ex(store, pid)))
    store.set_run_winner(run_id, winner)
    sail = ResumeSail()
    resume = WorkerResume(cfg, store, sail)
    resume.keep(_ex(store, winner))

    resume.answer(loser, "why?")
    assert sail.woken == ["winner-box"]


@pytest.mark.parametrize("release", ["adopt_or_dismiss", "session_end", "ttl"])
def test_box_is_let_go(env, release):
    cfg, store = env
    proposal_id = seed_proposal(store, box_id="worker-box")
    sail = ResumeSail()
    resume = WorkerResume(cfg, store, sail)
    resume.keep(_ex(store, proposal_id))

    if release == "adopt_or_dismiss":
        resume.release_proposal(proposal_id)
    elif release == "session_end":
        resume.release_session("s1")
    else:
        resume.expire(now=time.time() + 3600)
        assert sail.terminated == []  # within the TTL: kept
        resume.expire(now=time.time()
                      + cfg.experiments.winner_box_ttl_hours * 3600 + 60)

    assert sail.terminated == ["worker-box"]
    assert store.kept_box("s1") is None
    assert resume.can_resume(proposal_id) is True  # the checkpoint remains


def test_newer_delivery_replaces_the_kept_box(env):
    cfg, store = env
    first = seed_proposal(store, box_id="old-box")
    second = seed_proposal(store, box_id="new-box")
    sail = ResumeSail()
    resume = WorkerResume(cfg, store, sail)
    resume.keep(_ex(store, first))
    resume.keep(_ex(store, second))
    assert sail.terminated == ["old-box"]
    assert store.kept_box("s1")["box_id"] == "new-box"


def test_release_only_touches_this_sessions_winner(env):
    cfg, store = env
    theirs = seed_proposal(store, session_id="s2", box_id="their-box")
    mine = seed_proposal(store, box_id="my-box")
    other = seed_proposal(store)
    sail = ResumeSail()
    resume = WorkerResume(cfg, store, sail)
    resume.keep(_ex(store, theirs))
    resume.keep(_ex(store, mine))

    resume.release_proposal(other)  # not the kept worker
    assert sail.terminated == []
    resume.release_proposal(mine)
    assert sail.terminated == ["my-box"]
    assert store.kept_box("s2")["box_id"] == "their-box"


def test_reaper_leaves_a_kept_box_alone(env):
    cfg, store = env
    proposal_id = seed_proposal(store, box_id="worker-box")
    ex_id = _ex(store, proposal_id)
    store.transition_exploration(ex_id, "DISCARDED", "seed")
    sail = ResumeSail()
    resume = WorkerResume(cfg, store, sail)
    resume.keep(ex_id)

    assert Reaper(sail, store).reap_once() == []
    store.clear_kept_box("s1")
    assert Reaper(sail, store).reap_once() == ["worker-box"]


def test_answer_relays_from_revived_worker(env):
    cfg, store = env
    proposal_id = seed_proposal(store)
    sail = ResumeSail()
    resume = WorkerResume(cfg, store, sail)

    out = resume.answer(proposal_id, "why did you move the counter?")

    assert out["source"] == "worker_resume"
    assert out["answer"] == "I moved the counter into the store."
    # The box was revived from the worker's checkpoint...
    assert sail.revived == ["cp-9"]
    # ...the driver got an answer-mode task with the question...
    task = json.loads(next(v for (b, p), v in sail.written.items()
                           if p == "/work/task.json"))
    assert task["mode"] == "answer"
    assert task["question"] == "why did you move the counter?"
    assert task["harness"] == "opencode"
    # ...the answer box was started and cleaned up...
    assert sail.started
    assert sail.terminated
    # ...and its box time was cost-recorded.
    costs = store._query("SELECT * FROM costs WHERE kind='box'")
    assert costs and costs[-1]["ref"].startswith("ex-")


def test_no_checkpoint_reports_unavailable_honestly(env):
    cfg, store = env
    proposal_id = seed_proposal(store, checkpoint_id=None)
    sail = ResumeSail()
    resume = WorkerResume(cfg, store, sail)

    out = resume.answer(proposal_id, "why?")
    assert out["source"] == "unavailable"
    assert out["answer"] is None
    assert sail.revived == []


def test_unknown_proposal_reports_unavailable(env):
    cfg, store = env
    resume = WorkerResume(cfg, store, ResumeSail())
    out = resume.answer("H-nope", "why?")
    assert out["source"] == "unavailable"


def test_timeout_cleans_up_and_reports(env):
    cfg, store = env
    proposal_id = seed_proposal(store)
    sail = ResumeSail(answer_after_polls=10_000)  # never answers
    resume = WorkerResume(cfg, store, sail)

    t0 = time.time()
    out = resume.answer(proposal_id, "why?", timeout=0.2)
    assert out["source"] == "timeout"
    assert out["answer"] is None
    assert sail.terminated  # no leaked answer box
    assert time.time() - t0 < 30


def test_can_resume(env):
    cfg, store = env
    resume = WorkerResume(cfg, store, ResumeSail())
    assert resume.can_resume(seed_proposal(store)) is True
    assert resume.can_resume(seed_proposal(store, checkpoint_id=None)) is False
    assert resume.can_resume("H-nope") is False


def test_executor_collect_checkpoints_then_sleeps_run_workers(tmp_path):
    from tests.unit.test_orchestrator_flow import FakeSailClient

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "app.py").write_text("x = 1\n")
    cfg = HindsightConfig(project_dir=repo, hindsight_home=tmp_path / "hs")
    store = Store(cfg.db_path)
    try:
        fake = FakeSailClient()
        run_id = store.create_run("s1", 1, ["R1"])
        store.transition_run(run_id, "PLANNED", "seed")
        ex_id = store.create_exploration(
            "s1", "clean_rewrite", "pi", "t1", 1,
            {"target_ids": ["R1"]}, run_id=run_id)
        store.transition_exploration(ex_id, "PROVISIONING", "seed")
        store.transition_exploration(ex_id, "RUNNING", "seed")
        box = fake.create_box(f"hs-{ex_id}")
        store.set_box_id(ex_id, box.id)

        from hindsight.executor import Executor, _RESUME_CHECKPOINT_TTL
        from hindsight.snapshot import Snapshotter
        from hindsight.config import DEFAULT_SNAPSHOT_EXCLUDES
        snap = Snapshotter(repo, cfg.snapshot_repo, DEFAULT_SNAPSHOT_EXCLUDES, 5.0)
        snap.ensure()
        executor = Executor(cfg, store, snap, fake, None)
        executor._collect(ex_id, box.id)

        assert [(c[0], c[1]) for c in fake.checkpoint_log] == \
            [(box.id, _RESUME_CHECKPOINT_TTL)]
        assert fake.checkpoint_log[0][2].startswith("hs-resume-")
        assert store.get_exploration(ex_id)["checkpoint_id"] == "cp-1"
        assert box.id in fake.slept
        assert box.id in fake.boxes  # asleep, not terminated
    finally:
        store.close()


def test_followup_continues_the_same_conversation(tmp_path):
    """A send-back runs in the same box and agent session, and the driver restarts
    clean so the old result is not mistaken for the new one."""
    from hindsight.executor import Executor

    repo = tmp_path / "repo"
    repo.mkdir()
    cfg = HindsightConfig(project_dir=repo, hindsight_home=tmp_path / "hs")
    store = Store(cfg.db_path)
    try:
        run_id = store.create_run("s1", 1, ["R1"])
        old_id = store.create_exploration("s1", "council_synthesis", "opencode",
                                          "t1", 1, {"target_ids": ["R1"]},
                                          run_id=run_id)
        store.set_box_id(old_id, "worker-box")

        class Sail(ResumeSail):
            commands: list = []

            def run(self, box_id, command, *, timeout=None, cwd=None, env=None):
                self.commands.append(" ".join(command))
                return ExecOut(0, "", "", False)

            def file_exists(self, box_id, path):
                return False

        sail = Sail()
        sail.written[("worker-box", "/work/task.json")] = json.dumps(
            {"exploration_id": old_id, "prompt": "old", "baseline_commit": "abc"})
        ex = Executor(cfg, store, None, sail, None)
        ex._monitor_only = lambda *a: None
        messages = [{"id": 7, "ts": 1.0, "text": "also round to cents"}]

        new_id = ex.submit_followup(store.get_exploration(old_id), messages)
        ex._run_followup(new_id, messages)

        task = json.loads(sail.written[("worker-box", "/work/task.json")])
        assert task["agent_session"] == f"ses-hs-worker-{old_id}"[:60]
        assert task["exploration_id"] == new_id
        assert task["baseline_commit"] == "abc"
        assert "also round to cents" in task["prompt"]
        assert any("rm -f /work/result.json" in c for c in sail.commands)
        assert sail.started == ["worker-box"]
        row = store.get_exploration(new_id)
        assert row["box_id"] == "worker-box" and row["state"] == "RUNNING"
        assert store.messages_unseen_by(new_id) == []
        ex.pool.shutdown(wait=True)
    finally:
        store.close()


def test_driver_answer_mode_contract(tmp_path, monkeypatch):
    """The in-box driver's answer mode writes answer.json from the harness
    reply. Runs the real standalone driver code against a temp /work."""
    from hindsight.box import driver as drv

    work = tmp_path / "work"
    work.mkdir()
    (work / "result.json").write_text(json.dumps({
        "summary": {"summary": "moved counts to a shared store",
                    "assumptions": []}}))
    monkeypatch.setattr(drv, "WORK", work)
    monkeypatch.setattr(drv, "REPO", tmp_path)
    monkeypatch.setattr(drv, "STATUS_PATH", work / "status.json")
    monkeypatch.setattr(drv, "EVENTS_PATH", work / "events.jsonl")
    monkeypatch.setattr(drv, "RESULT_PATH", work / "result.json")
    monkeypatch.setattr(drv, "JUNIT_PATH", work / "junit.xml")

    calls: list[dict] = []

    def fake_harness(task, monitor):
        calls.append(task)
        return {"exit_reason": "done", "agent_turns": 1,
                "input_tokens": 10, "output_tokens": 20,
                "final_text": "Because the store owns the counter."}

    monkeypatch.setitem(drv.HARNESSES, "pi", fake_harness)

    rc = drv.run_answer_mode({
        "mode": "answer", "exploration_id": "ex-1", "harness": "pi",
        "question": "Why move the counter?",
        "budget": {"max_wall_clock_s": 60},
    })

    assert rc == 0
    out = json.loads((work / "answer.json").read_text())
    assert out["answer"] == "Because the store owns the counter."
    assert out["exit_reason"] == "done"
    assert out["agent_turns"] == 1
    # The prompt carried the question and the worker's own summary.
    assert "Why move the counter?" in calls[0]["prompt"]
    assert "moved counts to a shared store" in calls[0]["prompt"]


def test_driver_answer_mode_records_harness_failure(tmp_path, monkeypatch):
    """A harness crash leaves an honest empty answer, never a fake one."""
    from hindsight.box import driver as drv

    work = tmp_path / "work"
    work.mkdir()
    (work / "result.json").write_text("{}")
    monkeypatch.setattr(drv, "WORK", work)
    monkeypatch.setattr(drv, "REPO", tmp_path)
    monkeypatch.setattr(drv, "STATUS_PATH", work / "status.json")
    monkeypatch.setattr(drv, "EVENTS_PATH", work / "events.jsonl")

    def broken(task, monitor):
        raise RuntimeError("agent down")

    monkeypatch.setitem(drv.HARNESSES, "pi", broken)
    drv.run_answer_mode({"mode": "answer", "exploration_id": "ex-1",
                         "harness": "pi", "question": "q?",
                         "budget": {"max_wall_clock_s": 5}})
    out = json.loads((work / "answer.json").read_text())
    assert out["answer"] == ""
    assert out["exit_reason"] == "error"
