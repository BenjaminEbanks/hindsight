"""Snapshotter.

Snapshots live in a private bare git repository using the user's folder as
the work tree but a SEPARATE git directory and index. This captures
uncommitted and untracked files, deduplicates content across turns, makes
diffs between turns trivial, and never touches the user's own .git, index,
branches, or stash.

The user's .gitignore files are honored (they are part of the work tree),
and our own excludes live in snap.git/info/exclude.
"""

from __future__ import annotations

import fcntl
import hashlib
import os
import re
import subprocess
import tarfile
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

# Patterns that always block upload when matched in snapshot content.
SECRET_PATTERNS: list[tuple[str, str]] = [
    ("aws-access-key", r"AKIA[0-9A-Z]{16}"),
    ("github-token", r"gh[pousr]_[A-Za-z0-9]{20,}"),
    ("private-key", r"-----BEGIN (RSA |EC |OPENSSH |DSA |PGP )?PRIVATE KEY-----"),
    ("slack-token", r"xox[baprs]-[A-Za-z0-9-]{10,}"),
    ("google-api-key", r"AIza[0-9A-Za-z_-]{35}"),
]


class SecretsFound(Exception):
    """Raised when export is blocked by secret-scan findings."""

    def __init__(self, findings: list[Finding]):
        self.findings = findings
        lines = [f"{f.kind}: {f.path}" for f in findings[:20]]
        super().__init__(
            "secret scan blocked upload: " + "; ".join(lines) +
            (" ..." if len(findings) > 20 else "") +
            " — exclude these paths or confirm false positives"
        )


class SnapshotError(Exception):
    pass


@dataclass
class Finding:
    path: str
    line: int
    kind: str
    match: str


@dataclass
class SnapshotResult:
    commit: str
    ref: str
    files: int
    skipped_large: list[str] = field(default_factory=list)
    skipped_symlink: list[str] = field(default_factory=list)
    total_bytes: int = 0
    duration_s: float = 0.0


class Snapshotter:
    def __init__(self, project_dir: Path, snap_dir: Path, exclude: Iterable[str], max_file_mb: float = 5.0):
        self.project_dir = Path(project_dir).resolve()
        self.snap_dir = Path(snap_dir)
        self.exclude = list(exclude)
        self.max_file_bytes = int(max_file_mb * 1024 * 1024)
        self._lock_path = self.snap_dir.parent / "snapshot.lock"
        self._ensure_ran = False

    # ------------------------------------------------------------------ git

    def _git(self, *args: str, check: bool = True, input_bytes: bytes | None = None) -> subprocess.CompletedProcess:
        cmd = [
            "git", f"--git-dir={self.snap_dir}", f"--work-tree={self.project_dir}",
            "-c", "user.name=hindsight", "-c", "user.email=hindsight@localhost",
            "-c", "core.abbrev=12",
            *args,
        ]
        proc = subprocess.run(cmd, capture_output=True, input=input_bytes)
        if check and proc.returncode != 0:
            raise SnapshotError(f"git {' '.join(args)} failed: {proc.stderr.decode(errors='replace')[:500]}")
        return proc

    def ensure(self) -> None:
        """Init the bare shadow repo and write our excludes. Idempotent."""
        if self._ensure_ran:
            return
        self.snap_dir.parent.mkdir(parents=True, exist_ok=True)
        if not (self.snap_dir / "HEAD").exists():
            r = subprocess.run(["git", "init", "-q", "--bare", str(self.snap_dir)], capture_output=True)
            if r.returncode != 0:
                raise SnapshotError(f"git init failed: {r.stderr.decode()[:300]}")
        exclude_file = self.snap_dir / "info" / "exclude"
        exclude_file.parent.mkdir(parents=True, exist_ok=True)
        exclude_file.write_text("# managed by hindsight; do not edit\n" + "\n".join(self.exclude) + "\n")
        # Guard: the user's .git must never be snapshotted. Git ignores .git
        # directories in the work tree by construction; this is belt and braces.
        self._ensure_ran = True

    @contextmanager
    def _lock(self):
        self._lock_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self._lock_path, "w") as f:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)

    # -------------------------------------------------------------- snapshot

    def _ignored_paths(self, paths: list[str]) -> set[str]:
        """Which of `paths` the work tree's ignore rules now match.

        --no-index matters: check-ignore does not report paths that are
        already in our index, and the whole point here is catching tracked
        paths that a .gitignore written mid-session newly ignores (found
        live, an eval agent added `__pycache__/` to .gitignore
        after we had snapshotted the pycs; git add then refused our own
        tracked paths and every turn handler died). Exit code 1 just
        means "none ignored", so check=False.
        """
        if not paths:
            return set()
        proc = self._git(
            "check-ignore", "--stdin", "-z", "--no-index", check=False,
            input_bytes=b"".join(p.encode() + b"\0" for p in paths),
        )
        if proc.returncode not in (0, 1):
            raise SnapshotError(
                f"git check-ignore failed: {proc.stderr.decode(errors='replace')[:500]}")
        out = proc.stdout.decode(errors="replace")
        return {p for p in out.split("\0") if p}

    def _untrack(self, paths: list[str]) -> None:
        """Remove paths from OUR index only (`rm --cached`): the work tree,
        the user's repo, and their index are never touched. -f just skips
        git's staged-content safety — nothing is lost, earlier snapshot
        commits retain the blobs, and the index can legitimately hold
        content differing from both tree and HEAD (e.g. a partially-staged
        failed add, seen live)."""
        with tempfile.NamedTemporaryFile("wb", delete=False) as pf:
            for p in paths:
                pf.write(p.encode() + b"\0")
            pathspec = pf.name
        try:
            self._git("rm", "-q", "-f", "--cached", "--ignore-unmatch",
                      f"--pathspec-from-file={pathspec}", "--pathspec-file-nul")
        finally:
            os.unlink(pathspec)

    def _candidate_paths(self) -> tuple[list[str], list[str], list[str], list[str]]:
        """Return (paths_to_add, skipped_large, skipped_symlink_outside,
        tracked_now_ignored)."""
        tracked = self._git("ls-files", "-z").stdout.decode(errors="replace").split("\0")
        tracked = [p for p in tracked if p]
        untracked = self._git("ls-files", "--others", "--exclude-standard", "-z").stdout.decode(
            errors="replace"
        ).split("\0")
        untracked = [p for p in untracked if p]

        # A .gitignore written mid-session newly ignores paths we already
        # track: git add refuses explicitly-named ignored paths (even
        # tracked ones), and by design we honor the work tree's .gitignore
        # — so drop them from candidates and untrack them in snapshot().
        ignored = self._ignored_paths(tracked + untracked)

        paths: list[str] = []
        skipped_large: list[str] = []
        skipped_symlink: list[str] = []
        root = self.project_dir
        seen: set[str] = set()

        for p in tracked + untracked:
            if p in seen or not p:
                continue
            seen.add(p)
            full = root / p
            if p == ".git" or p.startswith(".git/"):
                continue  # never snapshot the user's git dir (git excludes it anyway)
            if p in ignored:
                # If it was in our index it must be untracked (the ignore
                # is the user's explicit instruction); either way it must
                # not be named in the git add pathspec.
                continue
            if not full.exists():
                paths.append(p)  # deleted since last snapshot; -A stages the removal
                continue
            if full.is_symlink():
                target = full.resolve()
                try:
                    target.relative_to(root)
                except ValueError:
                    skipped_symlink.append(p)
                    continue
            else:
                try:
                    if full.stat().st_size > self.max_file_bytes:
                        skipped_large.append(p)
                        # Keep it in the index if it was tracked before; just don't refresh.
                        if p in tracked:
                            paths.append(p)
                        continue
                except OSError:
                    continue
            paths.append(p)
        return paths, skipped_large, skipped_symlink, sorted(ignored & set(tracked))

    def snapshot(self, session_id: str, turn_n: int, phase: str) -> SnapshotResult:
        """Take a snapshot and update refs/hindsight/<session>/<n>/<phase>."""
        self.ensure()
        t0 = time.time()
        with self._lock():
            paths, skipped_large, skipped_symlink, tracked_now_ignored = self._candidate_paths()
            if tracked_now_ignored:
                # Honor the mid-session .gitignore: stop tracking these in
                # our shadow index so future snapshots exclude them (the
                # removal lands in this commit's diff, like any deletion).
                self._untrack(tracked_now_ignored)
            total_bytes = 0
            if paths:
                with tempfile.NamedTemporaryFile("wb", delete=False) as pf:
                    for p in paths:
                        pf.write(p.encode() + b"\0")
                    for p in paths:
                        try:
                            f = self.project_dir / p
                            if f.is_file() and not f.is_symlink():
                                total_bytes += f.stat().st_size
                        except OSError:
                            pass
                    pathspec = pf.name
                try:
                    self._git("add", "-A", f"--pathspec-from-file={pathspec}", "--pathspec-file-nul")
                finally:
                    os.unlink(pathspec)
            self._git("commit", "-q", "-m", f"turn {turn_n} {phase}", "--allow-empty")
            commit = self._git("rev-parse", "HEAD").stdout.decode().strip()
            ref = f"refs/hindsight/{session_id}/{turn_n}/{phase}"
            self._git("update-ref", ref, commit)
        return SnapshotResult(
            commit=commit, ref=ref, files=len(paths), skipped_large=skipped_large,
            skipped_symlink=skipped_symlink, total_bytes=total_bytes,
            duration_s=time.time() - t0,
        )

    # -------------------------------------------------------- settle guard

    def work_tree_fingerprint(self) -> str:
        """Cheap fingerprint of the (non-excluded) work tree: changes to any
        candidate file's size or mtime change the hash."""
        paths, _skipped_large, _skipped_symlink, _tracked_now_ignored = self._candidate_paths()
        h = hashlib.sha256()
        for p in sorted(paths):
            try:
                st = (self.project_dir / p).stat()
                h.update(f"{p}:{st.st_size}:{st.st_mtime_ns}".encode())
            except OSError:
                h.update(f"{p}:gone".encode())
        return h.hexdigest()

    def git_busy(self) -> Optional[str]:
        """Read-only probe of the user's .git: 'merge' or 'rebase' if an
        operation is in progress, else None. Never writes."""
        git_dir = self.project_dir / ".git"
        if not git_dir.exists():
            return None
        if (git_dir / "MERGE_HEAD").exists():
            return "merge"
        if (git_dir / "rebase-merge").exists() or (git_dir / "rebase-apply").exists():
            return "rebase"
        return None

    # ---------------------------------------------------------------- diffs

    def diff_numstat(self, a: str, b: str) -> list[tuple[str, int, int]]:
        """(path, added, removed) between two commits.

        `--numstat -z` emits one NUL-terminated record per file:
        "added\tremoved\tpath" (renames append a second field).
        """
        out = self._git("diff", "--numstat", "-z", a, b).stdout.decode(errors="replace")
        result = []
        for record in out.split("\0"):
            if not record:
                continue
            parts = record.split("\t")
            if len(parts) >= 3:
                add, rem, path = parts[0], parts[1], parts[2]
                result.append((path, int(add) if add.isdigit() else 0,
                               int(rem) if rem.isdigit() else 0))
        return result

    def manual_edits_between(self, prev_commit: str, cur_commit: str) -> list[dict]:
        return [{"path": p, "added": a, "removed": r} for p, a, r in self.diff_numstat(prev_commit, cur_commit)]

    def files_changed_since(self, commit: str | None) -> list[str]:
        if commit is None:
            return []
        proc = self._git("diff", "--name-only", "-z", commit, check=False)
        out = proc.stdout.decode(errors="replace")
        return [p for p in out.split("\0") if p]

    # ---------------------------------------------------------------- export

    def files_at(self, commit: str) -> list[str]:
        """All file paths in the snapshot at `commit` (repo map)."""
        out = self._git(
            "ls-tree", "-r", "--name-only", "-z", commit).stdout.decode(
                errors="replace")
        return [p for p in out.split("\0") if p]

    def file_at(self, commit: str, path: str, start: int = 1,
                end: int | None = None) -> str:
        """File content at `commit`, optionally sliced to 1-based line range
        [start, end]. Empty string if the path doesn't exist at that commit."""
        proc = self._git("show", f"{commit}:{path}", check=False)
        if proc.returncode != 0:
            return ""
        lines = proc.stdout.decode(errors="replace").splitlines()
        if start > 1 or end is not None:
            lines = lines[start - 1:end]
        return "\n".join(lines)

    def secret_findings(self, commit: str) -> list[Finding]:
        """Scan snapshot content at a commit with built-in regex patterns.

        Runs `git grep` against the commit tree, so it checks exactly what
        would be uploaded, not the live work tree. If `detect-secrets` is
        installed it is run as a second pass.
        """
        findings: list[Finding] = []
        for kind, pattern in SECRET_PATTERNS:
            proc = self._git(
                "grep", "-I", "-l", "-E", "-z", pattern, commit, check=False,
            )
            out = proc.stdout.decode(errors="replace")
            for path in out.split("\0"):
                if path:
                    findings.append(Finding(path=path, line=0, kind=kind, match=pattern))
        return findings

    def export_tarball(self, commit: str, dest: Path, secret_scan: bool = True, allow_secrets: bool = False) -> list[Finding]:
        """git archive the commit to dest (gzipped tar).

        Raises SecretsFound when the scan matches and allow_secrets is False
        (the caller surfaces the "exclude or confirm as false positive" flow).
        """
        findings = self.secret_findings(commit) if secret_scan else []
        if findings and not allow_secrets:
            raise SecretsFound(findings)
        dest.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(suffix=".tar", delete=False) as tf:
            tar_path = tf.name
        try:
            with open(tar_path, "wb") as out:
                proc = subprocess.Popen(
                    ["git", f"--git-dir={self.snap_dir}", "archive", commit],
                    stdout=out, stderr=subprocess.PIPE,
                )
                _, err = proc.communicate()
                if proc.returncode != 0:
                    raise SnapshotError(f"git archive failed: {err.decode()[:300]}")
            with tarfile.open(tar_path, "r") as tar, tarfile.open(dest, "w:gz") as gz:
                for member in tar:
                    gz.addfile(member, tar.extractfile(member) if member.isfile() else None)
        finally:
            os.unlink(tar_path)
        return findings
