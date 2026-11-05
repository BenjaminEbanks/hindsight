"""Adoption engine tests.

The engine runs for real against temp project directories (real git / 
patch(1) subprocesses); only the Sail surface is absent — adoption is
entirely local.
"""

from __future__ import annotations

import json
import subprocess

import pytest
from fastapi.testclient import TestClient

from hindsight.adopt import AdoptManager, AdoptionError, _patch_paths
from hindsight.config import HindsightConfig
from hindsight.store import Store

PATCH = (
    "--- a/app.py\n"
    "+++ b/app.py\n"
    "@@ -1 +1 @@\n"
    "-x = 1\n"
    "+x = 2\n"
    "--- a/helper.py\n"
    "+++ b/helper.py\n"
    "@@ -0,0 +1 @@\n"
    "+def help():\n"
    "+    return 42\n"
)

BAD_PATCH = (
    "--- a/app.py\n"
    "+++ b/app.py\n"
    "@@ -1 +1 @@\n"
    "-definitely not the current line\n"
    "+x = 2\n"
)


@pytest.fixture()
def env(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "app.py").write_text("x = 1\n")
    cfg = HindsightConfig(project_dir=repo, hindsight_home=tmp_path / "hs")
    cfg.project.test_command = "python3 -c 'print(\"tests ok\")'"
    store = Store(cfg.db_path)
    manager = AdoptManager(cfg, store)
    yield cfg, store, manager, repo
    store.close()


def seed_proposal(store, patch=PATCH, qualified=True) -> str:
    ex_id = store.create_exploration(
        "s1", "clean_rewrite", "pi", "t1", 1, {"target_ids": ["R1"]}, run_id="")
    for to in ("PROVISIONING", "RUNNING", "TESTING", "GATING"):
        store.transition_exploration(ex_id, to, "seed")
    proposal_id = store.save_proposal(
        ex_id, patch, {"summary": "moved counts"},
        {"files": 2, "added": 3, "removed": 1})
    store.set_proposal_selection(proposal_id, qualified, [], ["test_c1 (R1)"])
    return proposal_id


def test_adopt_applies_tests_notifies_and_backs_up(env):
    cfg, store, manager, repo = env
    proposal_id = seed_proposal(store)

    out = manager.adopt(proposal_id)

    # Applied: the working tree changed.
    assert (repo / "app.py").read_text() == "x = 2\n"
    assert (repo / "helper.py").exists()
    # Backup: pre-image + patch + manifest on disk.
    backup = cfg.hindsight_dir / "adoptions" / out["adoption_id"]
    assert (backup / "files" / "app.py").read_text() == "x = 1\n"
    assert (backup / "patch.diff").read_text() == PATCH
    manifest = json.loads((backup / "manifest.json").read_text())
    assert manifest["proposal_id"] == proposal_id
    assert manifest["created_files"] == ["helper.py"]
    # Tests ran (really) and passed.
    assert out["test_passed"] is True
    assert "tests ok" in out["test_output_tail"]
    # The store recorded it: notified, applied, files.
    row = store.get_adoption(out["adoption_id"])
    assert row["notified"] == 1
    assert row["test_passed"] == 1
    assert json.loads(row["files_json"]) == ["app.py"]
    assert json.loads(row["created_files_json"]) == ["helper.py"]
    assert row["undone_at"] is None
    actions = store._query("SELECT * FROM user_actions WHERE action='adopt'")
    assert len(actions) == 1
    # The user got the undo command.
    assert out["undo"] == f"hindsight undo {out['adoption_id']}"


def test_undo_restores_exactly(env):
    cfg, store, manager, repo = env
    proposal_id = seed_proposal(store)
    out = manager.adopt(proposal_id)

    undone = manager.undo(out["adoption_id"])

    assert (repo / "app.py").read_text() == "x = 1\n"
    assert not (repo / "helper.py").exists()
    assert undone["restored"] == ["app.py"]
    assert undone["removed"] == ["helper.py"]
    assert store.get_adoption(out["adoption_id"])["undone_at"] is not None

    # Idempotent: a second undo is a no-op.
    again = manager.undo(out["adoption_id"])
    assert again["already_undone"] is True


def test_undo_last(env):
    cfg, store, manager, repo = env
    proposal_id = seed_proposal(store)
    out = manager.adopt(proposal_id)
    assert manager.undo("last")["adoption_id"] == out["adoption_id"]
    assert (repo / "app.py").read_text() == "x = 1\n"


def test_adopt_in_git_repo_uses_git_apply(env, tmp_path):
    cfg, store, manager, repo = env

    def git(*args):
        subprocess.run(["git", "-C", str(repo), *args], check=True,
                       capture_output=True)

    git("init", "-q")
    git("add", "-A")
    git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "base")

    proposal_id = seed_proposal(store)
    out = manager.adopt(proposal_id)
    assert out["test_passed"] is True
    # git sees the working-tree change.
    st = subprocess.run(["git", "-C", str(repo), "status", "--porcelain"],
                        capture_output=True, text=True).stdout
    assert "app.py" in st and "helper.py" in st

    undone = manager.undo(out["adoption_id"])
    assert (repo / "app.py").read_text() == "x = 1\n"
    assert not (repo / "helper.py").exists()


def test_adopt_refuses_disqualified_proposal(env):
    cfg, store, manager, repo = env
    proposal_id = seed_proposal(store, qualified=False)
    with pytest.raises(AdoptionError, match="disqualified"):
        manager.adopt(proposal_id)
    assert (repo / "app.py").read_text() == "x = 1\n"  # untouched


def test_adopt_refuses_unknown_proposal(env):
    cfg, store, manager, repo = env
    with pytest.raises(AdoptionError, match="unknown proposal"):
        manager.adopt("H-nope")


def test_failing_patch_leaves_repo_untouched(env):
    cfg, store, manager, repo = env
    proposal_id = seed_proposal(store, patch=BAD_PATCH)
    with pytest.raises(AdoptionError, match="did not apply cleanly"):
        manager.adopt(proposal_id)
    assert (repo / "app.py").read_text() == "x = 1\n"
    # No adoption row was recorded for the failed attempt.
    assert store.adoptions_list() == []


def test_failing_tests_are_reported_not_hidden(env):
    cfg, store, manager, repo = env
    cfg.project.test_command = "python3 -c 'raise SystemExit(3)'"
    proposal_id = seed_proposal(store)

    out = manager.adopt(proposal_id)

    # Applied anyway — but the honest outcome travels with it.
    assert out["test_passed"] is False
    assert store.get_adoption(out["adoption_id"])["test_passed"] == 0
    manager.undo(out["adoption_id"])


def test_post_adopt_test_disabled(env):
    cfg, store, manager, repo = env
    cfg.adopt.post_adopt_test = False
    out = manager.adopt(seed_proposal(store))
    assert out["test_passed"] is None
    assert store.get_adoption(out["adoption_id"])["test_passed"] is None


def test_patch_paths_parses_headers():
    assert _patch_paths(PATCH) == ["app.py", "helper.py"]
    assert _patch_paths("") == []


def test_api_adopt_undo_flow(env):
    from hindsight.api import create_app
    from hindsight.orchestrator import Orchestrator
    from tests.unit.test_orchestrator_flow import FakeSailClient, scripted_model_fn

    cfg, store, manager, repo = env
    # A second store/orchestrator on the same db is fine (read-only here
    # besides what we drive through the API).
    orch = Orchestrator(cfg, model_fn=scripted_model_fn("{}"),
                        sail=FakeSailClient())
    try:
        client = TestClient(create_app(orch))
        proposal_id = seed_proposal(store)

        r = client.post(f"/proposals/{proposal_id}/adopt",
                        json={"via": "plugin"})
        assert r.status_code == 200
        ad = r.json()
        assert ad["test_passed"] is True
        assert (repo / "app.py").read_text() == "x = 2\n"

        listing = client.get("/adoptions").json()
        assert [a["id"] for a in listing] == [ad["adoption_id"]]

        r = client.post(f"/adoptions/{ad['adoption_id']}/undo",
                        json={"via": "plugin"})
        assert r.status_code == 200
        assert (repo / "app.py").read_text() == "x = 1\n"

        # Unknown proposal -> 404; unknown undo -> 404.
        r = client.post("/proposals/H-nope/adopt", json={"via": "plugin"})
        assert r.status_code == 404
        r = client.post("/adoptions/ad-nope/undo", json={"via": "plugin"})
        assert r.status_code == 404
    finally:
        orch.shutdown()
