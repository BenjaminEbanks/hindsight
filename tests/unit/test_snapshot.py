"""Snapshotter unit tests.

The critical invariant: the user's git repo is byte-identical after many
snapshots — status, index, branches, stash, and config untouched.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from hindsight.config import DEFAULT_SNAPSHOT_EXCLUDES
from hindsight.snapshot import SecretsFound, Snapshotter


@pytest.fixture()
def user_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "project"
    repo.mkdir()
    run = subprocess.run(["git", "init", "-q", str(repo)], capture_output=True)
    assert run.returncode == 0
    (repo / ".gitignore").write_text("ignored_dir/\n*.log\n")
    (repo / "README.md").write_text("# test project\n")
    (repo / "app.py").write_text("print('hello')\n")
    (repo / "ignored_dir").mkdir()
    (repo / "ignored_dir" / "junk.txt").write_text("junk")
    (repo / "debug.log").write_text("log")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "initial"], check=True, capture_output=True)
    return repo


def _git_state(repo: Path) -> dict:
    def out(*args: str) -> str:
        return subprocess.run(["git", "-C", str(repo), *args],
                              capture_output=True, text=True).stdout
    refs = out("for-each-ref")
    stash = subprocess.run(["git", "-C", str(repo), "stash", "list"],
                           capture_output=True, text=True).stdout
    return {
        "status": out("status", "--porcelain"),
        "refs": refs,
        "stash": stash,
        "index_hash": out("ls-files", "-s"),
        "config": (repo / ".git" / "config").read_text(),
        "head": out("rev-parse", "HEAD"),
    }


def make_snap(tmp_path: Path, user_repo: Path, exclude=None, max_mb=5.0) -> Snapshotter:
    snap = Snapshotter(user_repo, tmp_path / "snap.git",
                       exclude if exclude is not None else DEFAULT_SNAPSHOT_EXCLUDES,
                       max_file_mb=max_mb)
    snap.ensure()
    return snap


def test_user_repo_untouched_after_100_snapshots(tmp_path, user_repo):
    """Invariant: the user's git status, index, branches, and
    stash are byte-identical after 100 snapshots of an unchanged tree, and
    snapshotting files the user staged/untracked leaves them exactly as the
    user left them."""
    snap = make_snap(tmp_path, user_repo)
    for i in range(100):
        snap.snapshot("s1", i + 1, "post")
    assert _git_state(user_repo) == {  # unchanged work tree
        k: v for k, v in _git_state(user_repo).items()}

    # user stages a file and leaves another untracked, then we snapshot
    (user_repo / "staged.txt").write_text("staged by user")
    (user_repo / "loose.txt").write_text("loose file")
    (user_repo / "app.py").write_text("print('user edit')\n")
    subprocess.run(["git", "-C", str(user_repo), "add", "staged.txt"],
                   check=True, capture_output=True)
    before = _git_state(user_repo)
    snap.snapshot("s1", 101, "post")
    snap.snapshot("s1", 101, "pre")
    after = _git_state(user_repo)
    assert before == after
    assert "staged.txt" in before["index_hash"]      # still staged by user
    assert "?? loose.txt" in after["status"]          # still untracked


def test_captures_uncommitted_and_untracked(user_repo, tmp_path):
    (user_repo / "untracked.txt").write_text("not in git\n")
    (user_repo / "app.py").write_text("print('modified')\n")
    snap = make_snap(tmp_path, user_repo)
    r = snap.snapshot("s1", 1, "post")
    out = subprocess.run(
        ["git", f"--git-dir={tmp_path / 'snap.git'}", "show", "--name-only", r.commit],
        capture_output=True, text=True).stdout
    assert "untracked.txt" in out
    assert "app.py" in out


def test_honors_user_gitignore(user_repo, tmp_path):
    snap = make_snap(tmp_path, user_repo)
    r = snap.snapshot("s1", 1, "post")
    out = subprocess.run(
        ["git", f"--git-dir={tmp_path / 'snap.git'}", "ls-tree", "-r", "--name-only", r.commit],
        capture_output=True, text=True).stdout
    assert "ignored_dir/junk.txt" not in out
    assert "debug.log" not in out
    assert "README.md" in out


def test_never_snapshots_users_git_dir(user_repo, tmp_path):
    snap = make_snap(tmp_path, user_repo)
    r = snap.snapshot("s1", 1, "post")
    out = subprocess.run(
        ["git", f"--git-dir={tmp_path / 'snap.git'}", "ls-tree", "-r", "--name-only", r.commit],
        capture_output=True, text=True).stdout
    assert ".git/" not in out


def test_excludes_secrets(user_repo, tmp_path):
    (user_repo / ".env").write_text("AWS_KEY=x\n")
    (user_repo / "normal.py").write_text("y = 1\n")
    snap = make_snap(tmp_path, user_repo)
    r = snap.snapshot("s1", 1, "post")
    tree = subprocess.run(
        ["git", f"--git-dir={tmp_path / 'snap.git'}", "ls-tree", "-r", "--name-only", r.commit],
        capture_output=True, text=True).stdout
    assert ".env" not in tree
    assert "normal.py" in tree


def test_secret_scan_blocks_export(user_repo, tmp_path):
    (user_repo / "leaked.py").write_text('key = "AKIAIOSFODNN7EXAMPLE"\n')
    snap = make_snap(tmp_path, user_repo, exclude=[])
    r = snap.snapshot("s1", 1, "post")
    with pytest.raises(SecretsFound):
        snap.export_tarball(r.commit, tmp_path / "out.tar.gz")


def test_secret_scan_clean_export(user_repo, tmp_path):
    snap = make_snap(tmp_path, user_repo)
    r = snap.snapshot("s1", 1, "post")
    snap.export_tarball(r.commit, tmp_path / "out.tar.gz")
    assert (tmp_path / "out.tar.gz").exists()


def test_size_limit_skips_large_files(user_repo, tmp_path):
    (user_repo / "big.bin").write_bytes(b"\0" * (2 * 1024 * 1024))
    (user_repo / "small.txt").write_text("ok")
    snap = make_snap(tmp_path, user_repo, max_mb=1.0)
    r = snap.snapshot("s1", 1, "post")
    tree = subprocess.run(
        ["git", f"--git-dir={tmp_path / 'snap.git'}", "ls-tree", "-r", "--name-only", r.commit],
        capture_output=True, text=True).stdout
    assert "big.bin" not in tree
    assert "small.txt" in tree
    assert "big.bin" in r.skipped_large


def test_refs_and_diff_between_turns(user_repo, tmp_path):
    snap = make_snap(tmp_path, user_repo)
    (user_repo / "a.py").write_text("a = 1\n")
    r1 = snap.snapshot("s1", 1, "post")
    (user_repo / "b.py").write_text("b = 2\n")
    r2 = snap.snapshot("s1", 2, "post")
    assert r1.commit != r2.commit
    ref = subprocess.run(
        ["git", f"--git-dir={tmp_path / 'snap.git'}", "rev-parse", f"refs/hindsight/s1/2/post"],
        capture_output=True, text=True).stdout.strip()
    assert ref == r2.commit
    edits = snap.manual_edits_between(r1.commit, r2.commit)
    assert any(e["path"] == "b.py" and e["added"] == 1 for e in edits)


def test_deletions_staged(user_repo, tmp_path):
    (user_repo / "gone.txt").write_text("bye")
    snap = make_snap(tmp_path, user_repo)
    r1 = snap.snapshot("s1", 1, "post")
    (user_repo / "gone.txt").unlink()
    r2 = snap.snapshot("s1", 2, "post")
    edits = snap.manual_edits_between(r1.commit, r2.commit)
    assert any(e["path"] == "gone.txt" for e in edits)


def test_works_in_non_git_dir(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / "readme.txt").write_text("no git here")
    snap = make_snap(tmp_path, plain)
    r = snap.snapshot("s1", 1, "post")
    tree = subprocess.run(
        ["git", f"--git-dir={tmp_path / 'snap.git'}", "ls-tree", "-r", "--name-only", r.commit],
        capture_output=True, text=True).stdout
    assert "readme.txt" in tree


def test_symlink_outside_skipped(tmp_path, user_repo):
    outside = tmp_path / "outside.txt"
    outside.write_text("secret")
    (user_repo / "link.txt").symlink_to(outside)
    snap = make_snap(tmp_path, user_repo)
    r = snap.snapshot("s1", 1, "post")
    assert "link.txt" in r.skipped_symlink


def test_gitignore_written_after_paths_tracked(tmp_path, user_repo):
    """Regression : an eval agent wrote `__pycache__/`
    into .gitignore AFTER we had already snapshotted the pycs. git add then
    refused to name our own tracked paths ("The following paths are ignored
    by one of your .gitignore files") and every turn handler died. A
    mid-session .gitignore must be honored: untrack the newly-ignored paths
    from OUR index, never name them in the pathspec, and leave the user's
    repo untouched."""
    (user_repo / "__pycache__").mkdir()
    (user_repo / "__pycache__" / "m.pyc").write_text("\x00\x01")
    snap = make_snap(tmp_path, user_repo)
    first = snap.snapshot("s1", 1, "pre")
    assert "__pycache__/m.pyc" in snap.files_at(first.commit)  # tracked

    # the agent writes .gitignore mid-session; the pyc keeps changing
    (user_repo / ".gitignore").write_text("ignored_dir/\n*.log\n__pycache__/\n")
    (user_repo / "__pycache__" / "m.pyc").write_text("\x02\x03")
    (user_repo / "app.py").write_text("print('v2')\n")
    # reproduce probe5's exact index state: a partially-staged failed add
    # left the shadow index differing from both work tree and HEAD
    subprocess.run(
        ["git", f"--git-dir={tmp_path / 'snap.git'}", f"--work-tree={user_repo}",
         "add", "-f", "--", "__pycache__/m.pyc"], check=True, capture_output=True)
    before = _git_state(user_repo)

    second = snap.snapshot("s1", 1, "post")  # must not raise

    assert "__pycache__/m.pyc" not in snap.files_at(second.commit)
    changed = snap.files_changed_since(first.commit)
    assert "__pycache__/m.pyc" in changed  # lands in the diff as a removal
    assert "app.py" in changed
    assert _git_state(user_repo) == before  # rm --cached touched only OUR index

    # ignored files are no longer part of the settle signal either
    fp = snap.work_tree_fingerprint()
    (user_repo / "__pycache__" / "m.pyc").write_text("\x09\x0a")
    assert snap.work_tree_fingerprint() == fp


def test_deleted_file_that_is_now_ignored(tmp_path, user_repo):
    """A tracked file that the user deletes AND newly ignores must still
    leave the index cleanly (untracked like any mid-session ignore)."""
    (user_repo / "gen.py").write_text("g = 1\n")
    snap = make_snap(tmp_path, user_repo)
    first = snap.snapshot("s1", 1, "pre")
    assert "gen.py" in snap.files_at(first.commit)

    (user_repo / "gen.py").unlink()
    (user_repo / ".gitignore").write_text("ignored_dir/\n*.log\ngen.py\n")

    second = snap.snapshot("s1", 1, "post")  # must not raise

    assert "gen.py" not in snap.files_at(second.commit)
    assert "gen.py" in snap.files_changed_since(first.commit)
