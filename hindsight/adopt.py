"""Adoption engine.

The ONLY component allowed to write to the user's repo, and only on an
explicit command (v1 rule: never modify the user's repo otherwise).

Flow: backup folder -> apply patch -> run tests -> notify the main agent
-> one-command undo.

  backup/       hindsight_dir/adoptions/<id>/
    patch.diff   the applied patch, verbatim
    manifest.json proposal id, file lists, test command, timestamps
    files/<path> pre-image of every file the patch touches (so undo can
                restore exactly, even in a non-git project)

Undo restores the pre-images and deletes files the patch created. It is
one command: `hindsight undo <adoption-id>` (or `--last`).
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
import time
import uuid
from pathlib import Path

from .config import HindsightConfig
from .store import Store

log = logging.getLogger("hindsight")

_TEST_TIMEOUT_SECONDS = 1800
_PATCH_HEADER_RE = re.compile(r"^\+\+\+ b/(.+)$", re.MULTILINE)


class AdoptionError(Exception):
    pass


def _patch_paths(patch: str) -> list[str]:
    """File paths from a unified diff's +++ headers."""
    return _PATCH_HEADER_RE.findall(patch)


class AdoptManager:
    def __init__(self, cfg: HindsightConfig, store: Store, *,
                 on_adopted=None):
        self.cfg = cfg
        self.store = store
        self.on_adopted = on_adopted  # (proposal_id) -> None

    # -------------------------------------------------------------- public

    def adopt(self, proposal_id: str, *, via: str = "cli") -> dict:
        """Apply a proposal's patch to the user's working tree, run the
        project tests, and record everything needed for a one-command undo."""
        p = self.store.get_proposal(proposal_id)
        if p is None:
            raise AdoptionError(f"unknown proposal {proposal_id}")
        if self.cfg.adopt.mode != "working-tree":
            raise AdoptionError(
                f"adopt mode '{self.cfg.adopt.mode}' is not implemented; "
                "only 'working-tree' exists (never fake a capability)")
        patch = p.get("patch") or ""
        if not patch.strip():
            raise AdoptionError("proposal has no patch")
        if p.get("qualified") is False:
            raise AdoptionError(
                "proposal was disqualified: it failed its check, so it can't be "
                "adopted whole. Make the edits you want yourself.")

        # --- backup BEFORE touching anything --------------------------------
        ad_id = f"ad-{uuid.uuid4().hex[:8]}"
        backup = self.cfg.hindsight_dir / "adoptions" / ad_id
        backup.mkdir(parents=True, exist_ok=True)
        paths = _patch_paths(patch)
        existing = [q for q in paths
                     if (self.cfg.project_dir / q).exists()]
        created = [q for q in paths if q not in existing]
        for rel in existing:
            src = self.cfg.project_dir / rel
            dst = backup / "files" / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
        (backup / "patch.diff").write_text(patch)
        ex = self.store.get_exploration(p["exploration_id"])
        session_id = ex["session_id"] if ex is not None else ""
        manifest = {
            "adoption_id": ad_id, "proposal_id": proposal_id,
            "session_id": session_id, "files": existing,
            "created_files": created,
            "test_command": self.cfg.project.test_command,
            "applied_at": time.time(),
        }
        (backup / "manifest.json").write_text(json.dumps(manifest, indent=2))

        # --- apply -----------------------------------------------------------
        self._apply(patch)

        # --- tests (honest outcome, whatever it is) ---------------------------
        test_passed = None
        tests_out = ""
        if self.cfg.adopt.post_adopt_test:
            test_passed, tests_out = self._run_tests()

        self.store.save_adoption(
            ad_id, proposal_id, session_id, str(backup), existing, created,
            self.cfg.project.test_command)
        self.store.set_adoption_tests(ad_id, test_passed, tests_out)
        # Notify the main agent: the response to this call is the in-chat
        # notification; the row is the durable record .
        self.store.mark_adoption_notified(ad_id, f"in_chat_via_{via}")
        self.store.record_user_action(proposal_id, "adopt",
                                      f"adoption {ad_id}", via=via)
        log.info("adopt: %s applied proposal %s (tests passed=%s)",
                 ad_id, proposal_id, test_passed)
        if self.on_adopted is not None:
            self.on_adopted(proposal_id)
        return {
            "adoption_id": ad_id,
            "proposal_id": proposal_id,
            "files_changed": existing,
            "files_created": created,
            "test_passed": test_passed,
            "test_output_tail": tests_out[-2000:],
            "backup_dir": str(backup),
            "undo": f"hindsight undo {ad_id}",
            "message": (
                "Adopted. The code in your project has changed; a "
                "background redesign is now in your working tree. "
                + ("" if test_passed else
                   "NOTE: tests did NOT pass after adoption — see the "
                   "output. ") + f"Undo any time with: hindsight undo {ad_id}"),
        }

    def undo(self, adoption_id: str = "last", *, via: str = "cli") -> dict:
        """Restore the pre-images and remove created files. Idempotent per
        adoption: undoing an already-undone adoption is a no-op."""
        row = (self.store.get_adoption(adoption_id)
               if adoption_id != "last" else self.store.latest_adoption())
        if row is None:
            raise AdoptionError(
                f"no adoption '{adoption_id}'"
                + ("" if adoption_id == "last" else " (try 'hindsight adoptions')"))
        ad_id = row["id"]
        if row["undone_at"] is not None:
            return {"adoption_id": ad_id, "restored": [], "removed": [],
                    "already_undone": True}

        backup = Path(row["backup_dir"])
        restored, removed = [], []
        for rel in json.loads(row["files_json"] or "[]"):
            src = backup / "files" / rel
            dst = self.cfg.project_dir / rel
            if not src.exists():
                raise AdoptionError(
                    f"backup pre-image missing for {rel}; cannot undo {ad_id}")
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            restored.append(rel)
        for rel in json.loads(row["created_files_json"] or "[]"):
            dst = self.cfg.project_dir / rel
            if dst.exists():
                dst.unlink()
                removed.append(rel)

        self.store.mark_adoption_undone(ad_id)
        self.store.record_user_action(row["proposal_id"], "undo",
                                      f"adoption {ad_id}", via=via)
        log.info("undo: %s restored %d file(s), removed %d",
                 ad_id, len(restored), len(removed))
        return {"adoption_id": ad_id, "restored": restored,
                "removed": removed, "already_undone": False}

    # ------------------------------------------------------------ internals

    def _apply(self, patch: str) -> None:
        """git apply inside a git repo, patch(1) otherwise. A check pass
        (--check / --dry-run) runs first so a failing patch never leaves
        the tree half-applied. No merging, no fuzz."""
        pd = self.cfg.project_dir
        patch_file = self.cfg.hindsight_dir / "adoptions" / ".staged.diff"
        patch_file.parent.mkdir(parents=True, exist_ok=True)
        patch_file.write_text(patch)
        try:
            if (pd / ".git").exists():
                base = ["git", "-C", str(pd), "apply"]
                check = base + ["--check", str(patch_file)]
                apply_ = base + [str(patch_file)]
            else:
                # -N: new-file mode; -r -: no .rej files; --dry-run first.
                base = ["patch", "-d", str(pd), "-p1", "-N", "-r", "-",
                        "-i", str(patch_file)]
                check = base + ["--dry-run"]
                apply_ = base
            r = subprocess.run(check, capture_output=True, text=True,
                               timeout=120)
            if r.returncode != 0:
                raise AdoptionError(
                    f"patch did not apply cleanly: {r.stderr[-500:]}")
            r = subprocess.run(apply_, capture_output=True, text=True,
                               timeout=120)
            if r.returncode != 0:
                raise AdoptionError(
                    f"patch failed after passing check: {r.stderr[-500:]}")
        finally:
            patch_file.unlink(missing_ok=True)

    def _run_tests(self) -> tuple[bool, str]:
        """Run the project's test command in the user's repo. The outcome is
        recorded honestly — a failing run does not block the adoption (the
        patch is already applied), it is reported."""
        cmd = self.cfg.project.test_command
        if not cmd:
            return False, "this project has no test command, so nothing was run"
        try:
            r = subprocess.run(cmd, shell=True, cwd=str(self.cfg.project_dir),
                               capture_output=True, text=True,
                               timeout=_TEST_TIMEOUT_SECONDS)
            out = (r.stdout or "") + (r.stderr or "")
            return r.returncode == 0, out[-8000:]
        except subprocess.TimeoutExpired:
            return False, "test command timed out"
        except OSError as e:
            return False, f"test command failed to start: {e}"
