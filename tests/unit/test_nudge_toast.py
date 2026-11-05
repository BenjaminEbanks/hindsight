"""Nudge-delivery tests: the plugin-toast inbound path.

The toast surface itself is live-verified (see docs/VERIFY.md); here the
release/pending/ack lifecycle is tested against the real store and API with
the usual fakes (UNIT TEST MOCKS for Sail and the model).
"""

from __future__ import annotations

import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

from hindsight.config import HindsightConfig
from hindsight.orchestrator import Orchestrator
from hindsight.sail_client import BoxHandle, ExecOut
from tests.unit.test_orchestrator_flow import FakeSailClient


class ToastSail(FakeSailClient):
    def create_box(self, name, *, image=None, from_checkpoint_id=None,
                   max_lifetime_seconds=None, size=None):
        return BoxHandle(id="box-1", name=name)

    def run(self, box_id, command, *, timeout=None, cwd=None, env=None):
        return ExecOut(0, "seedcommit\n", "", False)


@pytest.fixture()
def app(tmp_path):
    from hindsight.api import create_app

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "app.py").write_text("x = 1\n")
    cfg = HindsightConfig(project_dir=repo, hindsight_home=tmp_path / "hs")
    orch = Orchestrator(cfg, model_fn=lambda p: json.dumps({"ops": []}),
                        sail=ToastSail())
    yield TestClient(create_app(orch)), orch
    orch.shutdown()


def seed_nudge(store) -> tuple[str, str]:
    """Minimal proposal+exploration chain plus a released nudge (exactly
    what delivery._deliver_run leaves behind for the plugin to fetch)."""
    run_id = store.create_run("s-toast", 1, ["R1"])
    store.transition_run(run_id, "PLANNED", "seed")
    ex_id = store.create_exploration(
        "s-toast", "clean_rewrite", "pi", "t1", 1, {"target_ids": ["R1"]},
        run_id=run_id)
    for to in ("PROVISIONING", "RUNNING", "TESTING", "GATING", "NUDGE_READY"):
        store.transition_exploration(ex_id, to, "seed")
    proposal_id = store.save_proposal(
        ex_id, "--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-x = 1\n+x = 2\n",
        {"summary": "moved counts to a shared store"},
        {"files": 1, "added": 1, "removed": 1})
    store.save_nudge("nudge-1", proposal_id, "low",
                     "A better design is ready: shared store passes.")
    store.mark_nudge_shown("nudge-1", "plugin_toast")
    return "nudge-1", proposal_id


def test_pending_lifecycle_store(app):
    _, orch = app
    store = orch.store

    nudge_id, proposal_id = seed_nudge(store)

    pending = store.pending_toast_nudges()
    assert len(pending) == 1
    row = pending[0]
    assert row["id"] == nudge_id
    assert row["proposal_id"] == proposal_id
    assert row["session_id"] == "s-toast"  # rides along for the toast "Open"
    assert "shared store" in row["text"]
    assert row["shown_at"] is not None

    assert store.mark_nudge_acked(nudge_id, displayed=True)
    assert store.pending_toast_nudges() == []
    # Double-ack is rejected: acked nudges never redeliver.
    assert store.mark_nudge_acked(nudge_id) is False


def test_unreleased_nudge_is_not_pending(app):
    _, orch = app
    store = orch.store

    run_id = store.create_run("s-toast", 1, ["R1"])
    store.transition_run(run_id, "PLANNED", "seed")
    ex_id = store.create_exploration(
        "s-toast", "clean_rewrite", "pi", "t1", 1, {"target_ids": ["R1"]},
        run_id=run_id)
    proposal_id = store.save_proposal(ex_id, "diff", {"summary": "s"}, {})

    # Saved but never released (no shown_at): invisible to the plugin.
    store.save_nudge("nudge-2", proposal_id, "low", "held")
    assert store.pending_toast_nudges() == []


def test_api_pending_and_ack(app):
    client, orch = app
    store = orch.store

    assert client.get("/nudges/pending").json() == []

    nudge_id, proposal_id = seed_nudge(store)
    out = client.get("/nudges/pending").json()
    assert len(out) == 1
    assert out[0]["id"] == nudge_id
    assert out[0]["proposal_id"] == proposal_id
    assert out[0]["session_id"] == "s-toast"
    assert "shared store" in out[0]["text"]

    # The plugin acks with displayed=false (headless surface): the ack is
    # recorded anyway — a missing toast surface must not redeliver forever.
    r = client.post(f"/nudges/{nudge_id}/ack", json={"displayed": False})
    assert r.status_code == 200 and r.json() == {"ok": True}
    assert client.get("/nudges/pending").json() == []

    # Unknown or already-acked: 404, no state change.
    assert client.post(f"/nudges/{nudge_id}/ack").status_code == 404
    assert client.post("/nudges/nope/ack").status_code == 404


def test_debug_gui_sink(app, tmp_path, monkeypatch):
    """POST /debug/gui: the desktop window has no fs, so the GUI part
    posts log lines here; they land in ~/.hindsight/gui-plugin.log."""
    monkeypatch.setenv("HOME", str(tmp_path))
    client, _ = app

    r = client.post("/debug/gui", json={"lines": ["hello", "world"]})
    assert r.status_code == 200 and r.json() == {"ok": True}
    log = tmp_path / ".hindsight" / "gui-plugin.log"
    text = log.read_text()
    assert "hello" in text and "world" in text

    # Spam guard: at most 20 lines per post, each truncated.
    r = client.post("/debug/gui", json={"lines": ["x" * 900] * 25})
    assert r.status_code == 200
    lines = log.read_text().strip().splitlines()
    assert len(lines) == 2 + 20
    assert all(len(l) <= 27 + 500 for l in lines)


def test_migration_adds_ack_columns(tmp_path):
    """A pre-M-G store (nudges without ack columns) migrates in place."""
    db = tmp_path / "old.sqlite3"
    conn = sqlite3.connect(str(db))
    conn.execute(
        "CREATE TABLE nudges (id TEXT PRIMARY KEY, proposal_id TEXT, "
        "tier TEXT, text TEXT, shown_at REAL, channel TEXT)")
    conn.execute(
        "INSERT INTO nudges (id, proposal_id, tier, text, shown_at, channel) "
        "VALUES ('n-old', 'p1', 'low', 'text', 1.0, 'plugin_toast')")
    conn.commit()
    conn.close()

    from hindsight.store import Store

    store = Store(db)
    try:
        cols = {r["name"] for r in store._query("PRAGMA table_info(nudges)")}
        assert {"acked_at", "ack_displayed"} <= cols
        # The pre-existing released nudge can be acked by the upgraded
        # plugin (its proposal/exploration chain is gone, so it is not
        # JOIN-visible in pending_toast_nudges — but the ack UPDATE by id
        # works, which is what stops any redelivery after an upgrade).
        assert store.mark_nudge_acked("n-old")
        assert store.mark_nudge_acked("n-old") is False
    finally:
        store.close()
