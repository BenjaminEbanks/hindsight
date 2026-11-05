"""The design review: what it reads, and how its answer is taken."""

from __future__ import annotations

import json
import subprocess

import pytest

from hindsight.config import HindsightConfig
from hindsight.design_review import (build_prompt, parse_verdict, review,
                                     run_dismissed_key, run_reason_key)
from hindsight.snapshot import Snapshotter
from hindsight.store import Store


@pytest.fixture()
def env(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    cfg = HindsightConfig(project_dir=repo, hindsight_home=tmp_path / "hs")
    store = Store(tmp_path / "db.sqlite3")
    snap = Snapshotter(repo, tmp_path / "snap.git", cfg.all_snapshot_excludes(),
                       cfg.project.max_file_mb)
    snap.ensure()
    yield cfg, store, snap, repo
    store.close()


def turn(store, snap, n, text):
    turn_id = store.start_turn("s1", n, f"m{n}", "pre")
    store.add_messages("s1", turn_id, [{"id": f"m{n}", "role": "user", "text": text}])
    commit = snap.snapshot("s1", n, "post").commit
    store.complete_turn(turn_id, "ok", commit, snapshot_files=1)
    store.mark_turn_usable(turn_id, True, "settled")


def test_first_request_is_not_reviewed(env):
    cfg, store, snap, repo = env
    (repo / "a.py").write_text("x = 1\n")
    turn(store, snap, 1, "build it")
    assert build_prompt(store, snap, cfg, "s1") is None


def test_prompt_separates_latest_from_earlier_and_shows_the_code(env):
    cfg, store, snap, repo = env
    (repo / "a.py").write_text("STATE = {}\n")
    turn(store, snap, 1, "keep sessions")
    turn(store, snap, 2, "run on three servers")
    prompt = build_prompt(store, snap, cfg, "s1")
    assert prompt.index("keep sessions") < prompt.index("LATEST REQUEST:")
    assert prompt.index("LATEST REQUEST:") < prompt.index("run on three servers")
    assert "--- a.py ---\nSTATE = {}" in prompt
    assert "(nothing yet)" in prompt


def test_files_with_secrets_never_reach_the_model(env):
    cfg, store, snap, repo = env
    (repo / "a.py").write_text("x = 1\n")
    key = "AKIA" + "ABCDEFGHIJKLMNOP"
    (repo / "creds.py").write_text(f"KEY = '{key}'\n")
    turn(store, snap, 1, "one")
    turn(store, snap, 2, "two")
    prompt = build_prompt(store, snap, cfg, "s1")
    assert key not in prompt
    assert "--- a.py ---" in prompt


def test_code_over_the_limit_is_named_not_silently_lost(env):
    cfg, store, snap, repo = env
    cfg.experiments.review_max_code_chars = 50
    (repo / "small.py").write_text("x = 1\n")
    (repo / "zbig.py").write_text("y = 1\n" * 100)
    turn(store, snap, 1, "one")
    turn(store, snap, 2, "two")
    prompt = build_prompt(store, snap, cfg, "s1")
    assert "--- small.py ---" in prompt
    assert "--- zbig.py ---" not in prompt
    assert "too large to include: zbig.py" in prompt


def test_past_runs_and_what_the_user_turned_down_are_shown(env):
    cfg, store, snap, repo = env
    (repo / "a.py").write_text("x = 1\n")
    turn(store, snap, 1, "one")
    turn(store, snap, 2, "two")
    empty = store.create_run("s1", 0, ["T1"])
    store.kv_set(run_reason_key(empty), "use a queue")
    declined = store.create_run("s1", 0, ["T2"])
    store.kv_set(run_reason_key(declined), "use a database")
    store.kv_set(run_dismissed_key(declined), "wrong_direction")
    prompt = build_prompt(store, snap, cfg, "s1")
    assert "use a queue (no redesign came of it)" in prompt
    assert "use a database (offered to the user, who turned it down)" in prompt


def test_user_text_in_a_request_is_not_treated_as_a_template(env):
    cfg, store, snap, repo = env
    (repo / "a.py").write_text("x = 1\n")
    turn(store, snap, 1, "use the {code} and {latest} placeholders")
    turn(store, snap, 2, "two")
    assert "use the {code} and {latest} placeholders" in build_prompt(
        store, snap, cfg, "s1")


def test_verdict_comes_after_the_reasoning_fields():
    raw = ("Sure:\n```json\n" + json.dumps({
        "latest_request_needs": "a", "what_was_true_before": "b",
        "what_no_longer_holds": "a shared store",
        "call_background": True}) + "\n```")
    verdict = parse_verdict(raw)
    assert verdict.yes and verdict.reason == "a shared store"


@pytest.mark.parametrize("raw", ["", "no idea", '{"call_background": "yes"}', "{}"])
def test_unclear_or_failed_review_is_no_verdict(raw):
    assert review("p", lambda p: raw) is None


def test_model_failure_is_no_verdict():
    def fail(p):
        raise TimeoutError("timed out")
    assert review("p", fail) is None
