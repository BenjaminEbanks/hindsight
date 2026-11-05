"""Staged task format.

A task is a self-contained JSON manifest:

    {
      "id": "counters",
      "title": "Counter service that outgrows its design",
      "no_change": false,              # true => the right nudge count is 0
      "test_command": "...",           # the command the AGENT is told to use
      "hidden_test_command": "...",    # how hidden tests are executed (local)
      "initial_repo": {"app.py": "...", ...},
      "stages": [
        {
          "message": "what the simulated user says",
          "requirements": [{"id": "R1", "text": "..."}],   # ground truth
          "hidden_tests": {"test_shared.py": "..."}       # accumulated to the end
        }, ...
      ]
    }

Rules the format enforces (validated on load):
  - stage messages are the only user input (deterministic replay);
  - hidden tests never ship to the main agent or any worker box — the
    episode runner copies them into a temp dir at scoring time only;
  - ground-truth requirements accumulate across stages (the final ledger
    is scored against the union);
  - no-change tasks are just tasks whose later stages fit the initial
    design — the right number of nudges is zero.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from pydantic import BaseModel, Field, field_validator


class Requirement(BaseModel):
    id: str
    text: str


class Stage(BaseModel):
    message: str
    requirements: list[Requirement] = Field(default_factory=list)
    hidden_tests: dict[str, str] = Field(default_factory=dict)
    # A closing turn is delivered to the main agent but never observed by
    # Hindsight: it exists so a nudge from the last real stage can land,
    # and nothing built after it could reach the agent anyway.
    closing: bool = False


class Task(BaseModel):
    id: str
    title: str
    no_change: bool = False
    test_command: str = "python3 -m pytest -q"
    hidden_test_command: str = "python3 -m pytest -q {hidden_dir}"
    initial_repo: dict[str, str] = Field(default_factory=dict)
    stages: list[Stage]

    @field_validator("stages")
    @classmethod
    def _nonempty(cls, v: list[Stage]) -> list[Stage]:
        if not v:
            raise ValueError("a task needs at least one stage")
        return v

    # ------------------------------------------------------------ loading

    @classmethod
    def load(cls, path: str | Path) -> "Task":
        path = Path(path)
        task = cls.model_validate(json.loads(path.read_text()))
        if task.id != path.stem:
            raise ValueError(
                f"task id {task.id!r} does not match file name {path.stem!r}")
        return task

    @classmethod
    def load_dir(cls, directory: str | Path) -> list["Task"]:
        directory = Path(directory)
        return [cls.load(p) for p in sorted(directory.glob("*.json"))]

    # ------------------------------------------------------------ helpers

    def materialize(self, dest: str | Path) -> Path:
        """Write the initial repo into `dest` (must not exist yet).

        The repo gets its own `git init` + initial commit. This is not
        cosmetic: coding agents resolve their workspace by the nearest git
        root, so a staged repo without one silently becomes "whatever
        enclosing repo it happens to sit in" (found live: the
        eval agent wrote its code into the outer project and read that
        project's docs). It also makes the delivery path's read-only user
        HEAD check meaningful inside the episode."""
        dest = Path(dest)
        dest.mkdir(parents=True, exist_ok=False)
        for rel, content in self.initial_repo.items():
            f = dest / rel
            if ".." in f.parts:
                raise ValueError(f"unsafe repo path {rel!r}")
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(content)
        subprocess.run(
            ["git", "init", "-q"], cwd=str(dest), check=True,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run(
            ["git", "-C", str(dest), "add", "-A"], check=True,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run(
            ["git", "-C", str(dest), "-c", "user.name=eval",
             "-c", "user.email=eval@hindsight", "commit", "-qm",
             "staged task initial repo", "--allow-empty"],
            check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return dest

    def ground_truth(self) -> list[Requirement]:
        """Union of stage requirements (ledger F1 is scored against this)."""
        seen: dict[str, Requirement] = {}
        for s in self.stages:
            for r in s.requirements:
                seen[r.id] = r
        return list(seen.values())

    def hidden_tests(self) -> dict[str, str]:
        """Union of hidden tests (scored only at episode end)."""
        out: dict[str, str] = {}
        for s in self.stages:
            out.update(s.hidden_tests)
        return out

    def hidden_test_command_for(self, hidden_dir: str | Path) -> str:
        return self.hidden_test_command.format(hidden_dir=hidden_dir)


def closing_stage() -> Stage:
    """A requirement-free final user turn, given to EVERY compared arm.

    Relay rides the next user message, so without this a nudge built after
    the last stage can never reach the agent (39 of 120 recorded nudges),
    and final-stage hidden tests (14 of 29) can never be rescued. Giving it
    to baseline and full_context too keeps the extra turn from being a
    confound: every arm gets the same last chance to tidy up."""
    return Stage(
        message=("That's everything for now. Before we wrap up, please make "
                 "sure it all works and finish anything still outstanding."),
        closing=True)


def redesign_stage(task: Task) -> Stage:
    """Baseline B: the main agent is asked to redesign at
    the END of the session, handed the ground-truth requirement list. This
    is deliberately more information than the treatment ever gets — the
    baseline is strong, and Hindsight must beat it by catching the break
    EARLIER, not by knowing more."""
    reqs = "\n".join(f"- {r.text}" for r in task.ground_truth())
    return Stage(
        message=(
            "Before we finish, one more thing: please review the whole "
            "design against this list of requirements and rework anything "
            "that doesn't hold. Then make sure the tests pass.\n"
            f"Requirements:\n{reqs}"),
        requirements=[], hidden_tests={})
