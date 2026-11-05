"""Run provenance: which code, tasks and models produced a result.

Without this, results from different versions of the code or the task specs
sit in one table looking comparable (the mixed-spec expenses row; "hollow"
vintages that could only be identified from memory afterwards).
"""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

SHIPPED_PATHS = ("hindsight", "eval/tasks", "scripts",
                 "pyproject.toml", "README.md")

# What decides whether two results may share a table.
COMPARABLE_KEYS = ("content_sha256", "model", "worker_model", "reviewer_model",
                   "judge_model", "stage_gap")


def is_shipped(path: Path) -> bool:
    return "__pycache__" not in path.parts and path.suffix != ".pyc"


def content_hash(project: Path) -> str:
    """Hash of exactly what an episode box receives. Unlike a git SHA it
    also identifies an uncommitted tree."""
    digest = hashlib.sha256()
    for rel in SHIPPED_PATHS:
        root = project / rel
        files = [root] if root.is_file() else sorted(root.rglob("*"))
        for f in files:
            if f.is_file() and is_shipped(f):
                digest.update(str(f.relative_to(project)).encode())
                digest.update(b"\0")
                digest.update(f.read_bytes())
    return digest.hexdigest()


def git_state(project: Path) -> dict:
    def run(*args: str) -> str:
        r = subprocess.run(["git", "-C", str(project), *args],
                           capture_output=True, text=True)
        return r.stdout.strip() if r.returncode == 0 else ""
    return {"git_sha": run("rev-parse", "HEAD"),
            "git_dirty": bool(run("status", "--porcelain"))}


def build_provenance(project: Path, *, model: str, worker_model: str = "",
                     reviewer_model: str = "", judge_model: str = "",
                     stage_gap: str = "") -> dict:
    return {**git_state(project), "content_sha256": content_hash(project),
            "model": model, "worker_model": worker_model,
            "reviewer_model": reviewer_model, "judge_model": judge_model,
            "stage_gap": stage_gap}


def comparable(a: dict | None, b: dict | None) -> bool:
    """Results without provenance predate it: never assume they match."""
    if not a or not b:
        return False
    return all(a.get(k) == b.get(k) for k in COMPARABLE_KEYS)
