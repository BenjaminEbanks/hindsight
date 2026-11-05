"""`hindsight_ask` + dismissal-wiring tests. The API is exercised with the real Orchestrator + API app over
a FakeSailClient (UNIT TEST MOCK); WorkerResume runs for real against the
fake Sail surface.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from hindsight.config import HindsightConfig
from tests.unit.test_orchestrator_flow import FakeSailClient, scripted_model_fn


class AnswerSail(FakeSailClient):
    """FakeSailClient that also serves the worker-resume answer flow."""

    def __init__(self):
        super().__init__()
        self.revived: list[str] = []
        self.answer = {"question": "why?", "answer": "shared store owns it",
                       "exit_reason": "done", "agent_turns": 1,
                       "input_tokens": 5, "output_tokens": 7}

    def create_box(self, name, *, image=None, from_checkpoint_id=None,
                   max_lifetime_seconds=None, size=None):
        if from_checkpoint_id:
            self.revived.append(from_checkpoint_id)
        return super().create_box(name, image=image,
                                  from_checkpoint_id=from_checkpoint_id,
                                  max_lifetime_seconds=max_lifetime_seconds,
                                  size=size)

    def file_exists(self, box_id, path):
        if path == "/work/answer.json":
            return True
        return super().file_exists(box_id, path)

    def read_file(self, box_id, path):
        if path == "/work/answer.json":
            return json.dumps(self.answer).encode()
        return super().read_file(box_id, path)


@pytest.fixture()
def app(tmp_path):
    from hindsight.api import create_app
    from hindsight.orchestrator import Orchestrator

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "app.py").write_text("x = 1\n")
    cfg = HindsightConfig(project_dir=repo, hindsight_home=tmp_path / "hs")
    cfg.explore.poll_interval_seconds = 0.05
    fake_sail = AnswerSail()
    orch = Orchestrator(cfg, model_fn=scripted_model_fn("{}"), sail=fake_sail)
    yield TestClient(create_app(orch)), orch, fake_sail
    orch.shutdown()


def seed_proposal(orch, *, checkpoint_id="cp-9",
                  assumptions=None) -> str:
    store = orch.store
    run_id = store.create_run("s1", 1, ["R1"])
    store.transition_run(run_id, "PLANNED", "seed")
    ex_id = store.create_exploration(
        "s1", "clean_rewrite", "pi", "t1", 1, {"target_ids": ["R1"]},
        run_id=run_id)
    for to in ("PROVISIONING", "RUNNING", "TESTING", "GATING", "NUDGE_READY"):
        store.transition_exploration(ex_id, to, "seed")
    summary = {"summary": "moved counts to a shared store"}
    if assumptions is not None:
        summary["assumptions"] = assumptions
    proposal_id = store.save_proposal(
        ex_id, "--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-x = 1\n+x = 2\n",
        summary, {"files": 1, "added": 1, "removed": 1})
    store.set_proposal_selection(proposal_id, True, [], ["test_c1 (R1)"])
    if checkpoint_id:
        store.set_exploration_checkpoint(ex_id, checkpoint_id)
    return proposal_id


def test_ask_read_only_returns_checked_facts(app):
    client, orch, fake_sail = app
    proposal_id = seed_proposal(
        orch, assumptions=[{"assumption": "the store is process-local"}])

    out = orch.ask(proposal_id)

    assert out["proposal_id"] == proposal_id
    assert out["summary"]["summary"] == "moved counts to a shared store"
    assert out["assumptions"] == [{"assumption": "the store is process-local"}]
    assert out["requirement_flips"] == ["test_c1 (R1)"]
    assert out["regressions"] == []
    assert out["qualified"] is True
    assert "-x = 1" in out["diff"]
    assert out["resumable"] is True
    # Strictly read-only: nothing was launched, written, or revived.
    assert fake_sail.revived == []
    assert fake_sail.written_files == {}


def test_ask_with_question_resumes_worker(app):
    client, orch, fake_sail = app
    proposal_id = seed_proposal(orch)

    out = orch.ask(proposal_id, "why did you move the counter?")

    assert out["question"] == "why did you move the counter?"
    assert out["answer"]["source"] == "worker_resume"
    assert out["answer"]["answer"] == "shared store owns it"
    assert fake_sail.revived == ["cp-9"]
    # The revived box got an answer-mode task and was cleaned up.
    assert "/work/task.json" in fake_sail.written_files
    assert json.loads(fake_sail.written_files["/work/task.json"])["mode"] == "answer"
    assert not fake_sail.boxes


def test_ask_without_checkpoint_is_honest(app):
    client, orch, fake_sail = app
    proposal_id = seed_proposal(orch, checkpoint_id=None)

    out = orch.ask(proposal_id, "why?")
    assert out["answer"]["source"] == "unavailable"
    assert out["answer"]["answer"] is None
    assert fake_sail.revived == []


def test_ask_unknown_proposal(app):
    client, orch, _ = app
    assert "error" in orch.ask("H-nope")


def test_api_ask_endpoints(app):
    client, orch, _ = app
    proposal_id = seed_proposal(orch)

    r = client.post(f"/proposals/{proposal_id}/ask", json={"question": None})
    assert r.status_code == 200
    assert r.json()["proposal_id"] == proposal_id
    assert "answer" not in r.json()

    r = client.post(f"/proposals/{proposal_id}/ask",
                    json={"question": "why?"})
    assert r.status_code == 200
    assert r.json()["answer"]["source"] == "worker_resume"

    r = client.post("/proposals/H-nope/ask", json={"question": None})
    assert r.status_code == 404


def test_api_dismiss_records_dismissal_memory(app):
    """The dismiss endpoint feeds the dismissal fingerprint
    — the M-D(b) gap: it used to log user_actions only."""
    client, orch, _ = app
    proposal_id = seed_proposal(orch)

    r = client.post(f"/proposals/{proposal_id}/dismiss?reason=wrong_direction")
    assert r.status_code == 200

    dismissals = orch.store._query("SELECT * FROM dismissals")
    assert len(dismissals) == 1
    fp = json.loads(dismissals[0]["fingerprint"])
    assert fp["strategy"] == "clean_rewrite"
    assert dismissals[0]["reason"] == "wrong_direction"
    actions = orch.store._query(
        "SELECT * FROM user_actions WHERE action='dismiss'")
    assert len(actions) == 1


def test_api_explorations_body_parses(app):
    """Regression: request models must live at module level — with postponed
    annotations FastAPI cannot resolve closure-local models, and the body
    was silently dropped (this exact bug hit the ask endpoint)."""
    client, orch, _ = app

    # No explorations actually launch (no snapshot/ledger in this fixture),
    # but the body must be parsed — session_id reaches the store query
    # instead of crashing on req=None.
    r = client.post("/explorations", json={"session_id": "s1",
                                           "strategies": ["clean_rewrite"]})
    assert r.status_code in (200, 409)
    if r.status_code == 200:
        assert "exploration_ids" in r.json()
