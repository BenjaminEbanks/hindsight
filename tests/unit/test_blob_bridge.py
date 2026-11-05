"""Regression tests for the worker-patch -> rebase-box bridge (probe 3j,
2026-11-02): all proposals were discarded "rebase conflict" because
(a) patches carried regenerated __pycache__ binaries that cannot be
3-way merged, and (b) the fresh rebox repo lacked the patch's pre-image
blobs, so `git apply --3way` degenerated to literal context matching and
ordinary same-file drift failed exactly like a true conflict.

Fix: git-info excludes in the worker baseline (no junk in patches) +
driver ships per-file pre-images which selection hash-objects into the
rebox before applying. These tests pin the invariant end to end at the
git level, using real repos in tmp dirs.
"""

from __future__ import annotations

import subprocess
import tarfile
from pathlib import Path

import pytest

from hindsight.box import driver as drv


def git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    r = subprocess.run(["git", "-C", str(repo), *args], capture_output=True)
    return r


def init_repo(path: Path, junk_exclude: bool) -> str:
    path.mkdir(parents=True, exist_ok=True)
    git(path, "init", "-q", ".")
    if junk_exclude:
        (path / ".git" / "info" / "exclude").write_text(
            "__pycache__/\n*.pyc\n.pytest_cache/\n")
    git(path, "add", "-A")
    git(path, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "base")
    return git(path, "rev-parse", "HEAD").stdout.decode().strip()


BASE = "def get(k):\n    return None\n"


def test_preimages_exclude_junk_and_new_files(tmp_path: Path):
    """write_preimages ships baseline blobs for CHANGED files only: the
    regenerated .pyc never appears in the patch NOR the preimages."""
    repo = tmp_path / "worker"
    (repo).mkdir()
    (repo / "cache.py").write_text(BASE)
    baseline = init_repo(repo, junk_exclude=True)

    # worker changes code; pytest runs and regenerates __pycache__
    (repo / "cache.py").write_text("def get(k):\n    return CACHE.get(k)\n")
    (repo / "__pycache__").mkdir()
    (repo / "__pycache__" / "cache.cpython-311.pyc").write_bytes(b"PYC")
    (repo / "new_mod.py").write_text("X = 1\n")  # added file: no preimage

    git(repo, "add", "-A")
    patch = git(repo, "diff", baseline).stdout.decode()
    assert "pyc" not in patch  # junk excluded from the patch itself

    orig_work, orig_repo = drv.WORK, drv.REPO
    drv.WORK, drv.REPO = tmp_path / "work", repo
    drv.WORK.mkdir()
    try:
        drv.write_preimages(baseline)
    finally:
        drv.WORK, drv.REPO = orig_work, orig_repo

    with tarfile.open(tmp_path / "work" / "preimages.tar.gz") as tar:
        names = tar.getnames()
        assert "preimages/cache.py" in names
        assert not any("pyc" in n for n in names)
        assert not any("new_mod" in n for n in names)


def test_blob_bridge_turns_drift_into_a_merge(tmp_path: Path):
    """The full bridge flow: worker patch + preimage -> drifted matrix repo
    -> hash-object preimage -> --3way merges non-overlapping same-file drift
    (pre-fix: 'repository lacks the necessary blob' -> conflict/discard)."""
    worker = tmp_path / "worker"
    worker.mkdir()
    (worker / "cache.py").write_text(BASE + "\n\ndef put(k, v):\n    pass\n")
    baseline = init_repo(worker, junk_exclude=True)
    (worker / "cache.py").write_text(
        "def get(k):\n    return CACHE.get(k)\n\n\nCACHE = {}\n\n\n"
        "def put(k, v):\n    pass\n")
    git(worker, "add", "-A")
    patch = git(worker, "diff", baseline).stdout.decode()
    preimage = git(worker, "show", f"{baseline}:cache.py").stdout

    matrix = tmp_path / "matrix"
    matrix.mkdir()
    # main agent appended a NEW function (same file, different region)
    (matrix / "cache.py").write_text(
        BASE + "\n\ndef put(k, v):\n    pass\n\n\ndef delete(k):\n    pass\n")
    init_repo(matrix, junk_exclude=True)
    (matrix / "patch.diff").write_text(patch)

    # without the bridge: missing ancestor blob
    r = git(matrix, "apply", "--3way", "patch.diff")
    lacks_blob = "lacks the necessary blob" in r.stderr.decode()

    git(matrix, "checkout", "-q", "--", ".")
    # with the bridge: hash-object the pre-image, then apply
    p = subprocess.run(["git", "--git-dir", str(matrix / ".git"),
                        "hash-object", "-w", "--stdin"],
                       input=preimage, capture_output=True)
    assert p.returncode == 0
    r = git(matrix, "apply", "--3way", "patch.diff")
    assert r.returncode == 0, r.stderr.decode()
    merged = (matrix / "cache.py").read_text()
    assert "CACHE.get(k)" in merged and "def delete" in merged
    if not lacks_blob:  # environment note, never a failure condition
        pytest.skip("direct-apply fallback happened to succeed here")


def test_true_overlap_still_drops(tmp_path: Path):
    """The drops we KEEP: both sides rewrote the same lines. The bridge
    gives --3way its ancestor and git reports a REAL conflict (exit != 0)
    — selection discards, never hand-merges."""
    worker = tmp_path / "worker"
    worker.mkdir()
    (worker / "cache.py").write_text(BASE)
    baseline = init_repo(worker, junk_exclude=True)
    (worker / "cache.py").write_text("def get(k):\n    return WORKER\n")
    git(worker, "add", "-A")
    patch = git(worker, "diff", baseline).stdout.decode()
    preimage = git(worker, "show", f"{baseline}:cache.py").stdout

    matrix = tmp_path / "matrix"
    matrix.mkdir()
    (matrix / "cache.py").write_text("def get(k):\n    return MAIN_AGENT\n")
    init_repo(matrix, junk_exclude=True)
    (matrix / "patch.diff").write_text(patch)
    subprocess.run(["git", "--git-dir", str(matrix / ".git"),
                    "hash-object", "-w", "--stdin"],
                   input=preimage, capture_output=True)
    r = git(matrix, "apply", "--3way", "patch.diff")
    assert r.returncode != 0
    assert "conflict" in r.stderr.decode().lower()
