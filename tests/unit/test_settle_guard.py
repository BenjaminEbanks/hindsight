"""Settle guard tests."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from hindsight.config import DEFAULT_SNAPSHOT_EXCLUDES
from hindsight.snapshot import Snapshotter


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "project"
    r.mkdir()
    (r / "app.py").write_text("x = 1\n")
    return r


def make_snap(tmp_path, repo, **kw) -> Snapshotter:
    snap = Snapshotter(repo, tmp_path / "snap.git",
                       DEFAULT_SNAPSHOT_EXCLUDES, **kw)
    snap.ensure()
    return snap


def test_fingerprint_stable_when_untouched(repo, tmp_path):
    snap = make_snap(tmp_path, repo)
    a = snap.work_tree_fingerprint()
    b = snap.work_tree_fingerprint()
    assert a == b


def test_fingerprint_changes_on_edit(repo, tmp_path):
    snap = make_snap(tmp_path, repo)
    a = snap.work_tree_fingerprint()
    (repo / "app.py").write_text("x = 2\n")
    assert snap.work_tree_fingerprint() != a


def test_fingerprint_changes_on_new_file(repo, tmp_path):
    snap = make_snap(tmp_path, repo)
    a = snap.work_tree_fingerprint()
    (repo / "new.py").write_text("y = 1\n")
    assert snap.work_tree_fingerprint() != a


def test_git_busy_none_without_git(repo, tmp_path):
    snap = make_snap(tmp_path, repo)
    assert snap.git_busy() is None


def test_git_busy_detects_merge(repo, tmp_path):
    subprocess.run(["git", "init", "-q", str(repo)], check=True,
                  capture_output=True)
    git_dir = repo / ".git"
    (git_dir / "MERGE_HEAD").write_text("0" * 40)
    snap = make_snap(tmp_path, repo)
    assert snap.git_busy() == "merge"


def test_git_busy_detects_rebase(repo, tmp_path):
    subprocess.run(["git", "init", "-q", str(repo)], check=True,
                  capture_output=True)
    (repo / ".git" / "rebase-apply").mkdir()
    snap = make_snap(tmp_path, repo)
    assert snap.git_busy() == "rebase"


def test_git_busy_clean_git_repo(repo, tmp_path):
    subprocess.run(["git", "init", "-q", str(repo)], check=True,
                  capture_output=True)
    snap = make_snap(tmp_path, repo)
    assert snap.git_busy() is None
