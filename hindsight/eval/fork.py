"""Fork-at-nudge measurement.

"How often a shown design actually helped": when a treatment episode
delivers a nudge with stages still to come, fork the session there —
copy the repo and the event store — and run the remaining stages twice:

  - ignore branch: continue as if the user never saw the nudge;
  - accept branch: ADOPT the winning proposal through the real adoption
    engine (the product's accept path), then continue.

Both branches run with hindsight OFF (the treatment already happened;
the fork measures the nudge's effect, not a second treatment). The
nudge helped when the accept branch passes the hidden tests and the
ignore branch does not.

Live-agent fidelity note: the forked branches each start a fresh main
agent (a scripted agent replays its remaining script; the OpenCode
agent starts a new session). The repo carries the design state, but
the branch agents do not inherit the conversation transcript.
Whether `opencode run --fork` returns a continuable
session id, which would let live branches inherit the transcript.
"""

from __future__ import annotations

import json
import logging
import shutil
from dataclasses import dataclass
from pathlib import Path

from .episode import EvalEpisode, EpisodeResult
from .tasks import Task

log = logging.getLogger("hindsight")


@dataclass
class ForkOutcome:
    task_id: str
    nudge_stage: int          # 0-based stage whose quiet wait delivered it
    proposal_id: str
    nudge_text: str
    accept: EpisodeResult
    ignore: EpisodeResult
    adopted: bool = True
    adopt_test_passed: bool | None = None
    adopt_error: str = ""

    @property
    def helped(self) -> bool:
        """The accept branch passes the hidden tests and the ignore branch
        does not — the nudge changed the final outcome for the better."""
        a, i = self.accept.final, self.ignore.final
        if a is None or i is None or not a.ran or not i.ran:
            return False
        # `ok`, not failed == 0: a run that collected no tests is not a pass.
        return a.ok and not i.ok

    @property
    def delta_passed(self) -> int:
        """Graded view: how many more hidden tests the accept branch passes
        (negative = the nudge made things worse). Counted from passes: a
        collection error is one failure per FILE, so failure counts are not
        comparable between branches."""
        if self.accept.final is None or self.ignore.final is None:
            return 0
        return self.accept.final.passed - self.ignore.final.passed


def _snapshot_store(episode: EvalEpisode, dest: Path) -> None:
    """Copy the live store db (checkpoint WAL first) so the accept branch
    can adopt the winning proposal through the real adoption engine."""
    store = episode.orch.store
    with store._lock:
        try:
            store._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except Exception:  # noqa: BLE001 - best effort; copy WAL too below
            log.warning("fork: WAL checkpoint failed; copying WAL as-is")
        shutil.copy2(store.db_path, dest)
        for ext in ("-wal", "-shm"):
            side = Path(str(store.db_path) + ext)
            if side.exists() and side.stat().st_size > 0:
                shutil.copy2(side, Path(str(dest) + ext))


def run_forked_episode(
    task: Task,
    repo_dir: str | Path,
    *,
    agent_factory,
    agent_script: list[dict],
    root_dir: str | Path,
    sail=None,
    model_fn=None,
    judge_model_fn=None,
    settings_overrides: dict | None = None,
    models_override: dict[str, str] | None = None,
    quiet_timeout: float = 2400.0,
) -> tuple[EpisodeResult, ForkOutcome | None, str]:
    """Run the treatment episode; if a nudge is delivered while stages
    remain, fork there and run accept/ignore branches on the remaining
    stages.

    agent_factory(repo_dir, script_slice) -> MainAgentHarness builds a
    fresh agent for a branch (scripted replay in tests; the live agent
    in the real eval).

    Returns (treatment_result, fork_outcome_or_None, note). The note is
    "" when a fork happened, else the honest reason it did not.
    """
    root = Path(root_dir)
    repo_dir = Path(repo_dir)
    fork_dir = root / "fork"
    state: dict = {"stage": None, "nudge": None}

    def on_nudge(nudge: dict, stage_index: int) -> None:
        if state["nudge"] is not None:
            return  # fork at the FIRST nudge only
        state["stage"], state["nudge"] = stage_index, nudge
        fork_dir.mkdir(parents=True, exist_ok=True)
        if fork_dir.joinpath("repo").exists():
            shutil.rmtree(fork_dir / "repo")
        shutil.copytree(repo_dir, fork_dir / "repo")
        _snapshot_store(episode, fork_dir / "store.sqlite3")
        (fork_dir / "fork.json").write_text(json.dumps(
            {"stage": stage_index, "nudge": nudge}, indent=2))
        log.info("fork: nudge delivered at stage %d; session forked",
                 stage_index + 1)

    agent = agent_factory(repo_dir, list(agent_script))
    episode = EvalEpisode(
        task, repo_dir, main_agent=agent, hindsight=True,
        hindsight_home=root / "hs", sail=sail, model_fn=model_fn,
        judge_model_fn=judge_model_fn,
        settings_overrides=settings_overrides,
        models_override=models_override, quiet_timeout=quiet_timeout,
        arm="treatment", on_nudge=on_nudge)
    treatment = episode.run()

    if state["nudge"] is None:
        note = ("no nudge delivered while stages remained — nothing to "
                "fork (for no-change tasks this is the correct outcome)")
        return treatment, None, note
    k = state["stage"]
    remaining = task.stages[k + 1:]
    if not remaining:
        note = ("nudge delivered at the final stage boundary — no "
                "remaining stages to measure a fork on")
        return treatment, None, note

    proposal_id = state["nudge"]["proposal_id"]

    # --- ignore branch: continue, never saw the nudge ---------------------
    ignore = _run_branch(task, fork_dir / "repo", root / "ignore",
                         agent_factory, agent_script[k + 1:], remaining,
                         arm="fork_ignore")

    # --- accept branch: adopt the proposal (real path), then continue ------
    accept_dir = root / "accept"
    if accept_dir.exists():
        shutil.rmtree(accept_dir)
    shutil.copytree(fork_dir / "repo", accept_dir / "repo")
    adopted, adopt_test_passed, adopt_error = _adopt_into(
        task, accept_dir / "repo", accept_dir / "hs",
        fork_dir / "store.sqlite3", proposal_id)
    agent = agent_factory(accept_dir / "repo", agent_script[k + 1:])
    accept = EvalEpisode(task, accept_dir / "repo", main_agent=agent,
                          hindsight=False, arm="fork_accept") \
        .run(stages=remaining)

    outcome = ForkOutcome(
        task_id=task.id, nudge_stage=k, proposal_id=proposal_id,
        nudge_text=state["nudge"]["text"], accept=accept, ignore=ignore,
        adopted=adopted, adopt_test_passed=adopt_test_passed,
        adopt_error=adopt_error)
    return treatment, outcome, ""


def _run_branch(task: Task, fork_repo: Path, branch_dir: Path,
                agent_factory, script_slice: list[dict], stages: list,
                *, arm: str) -> EpisodeResult:
    if branch_dir.exists():
        shutil.rmtree(branch_dir)
    shutil.copytree(fork_repo, branch_dir / "repo")
    agent = agent_factory(branch_dir / "repo", list(script_slice))
    episode = EvalEpisode(task, branch_dir / "repo", main_agent=agent,
                          hindsight=False, arm=arm)
    return episode.run(stages=stages)


def _adopt_into(task: Task, accept_repo: Path, accept_hs: Path,
                fork_store: Path, proposal_id: str) -> tuple[bool, bool | None, str]:
    """Apply the winning proposal to the accept branch's repo through the
    REAL adoption engine, run its post-adopt tests, and report honestly."""
    from ..adopt import AdoptionError, AdoptManager
    from ..config import HindsightConfig
    from ..store import Store

    store = Store(fork_store)
    try:
        cfg = HindsightConfig(project_dir=accept_repo, hindsight_home=accept_hs)
        cfg.project.test_command = task.test_command
        mgr = AdoptManager(cfg, store)
        result = mgr.adopt(proposal_id, via="eval_fork")
        return True, result.get("test_passed"), ""
    except AdoptionError as e:
        return False, None, str(e)
    except Exception as e:  # noqa: BLE001 - recorded honestly, never faked
        return False, None, f"{type(e).__name__}: {e}"
    finally:
        store.close()
