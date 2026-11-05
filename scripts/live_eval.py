"""Live eval runner.

Runs staged tasks against the REAL machinery: the local OpenCode CLI as
the main agent, real Sail boxes for workers, real model calls — all
cost-recorded. Arms:

  baseline_alone    the main agent by itself (arm 1: streamed context);
  full_context      arm 2: every request stated up front, one turn,
                   hindsight off — the information ceiling;
  baseline_redesign baseline B: a final "redesign against these
                   requirements" stage built from ground truth;
  treatment         the full Hindsight pipeline;
  treatment_relay   treatment + text-only nudge relayed by the user;
  treatment_diff    arm 3: treatment + the FULL DIFF delivered to the
                   main agent (agent-mediated; it decides whether to
                   incorporate);
  fork             treatment + fork-at-nudge accept/ignore branches.

Usage:
  uv run python scripts/live_eval.py --arms baseline_alone treatment \
      --tasks eval/tasks/counters.json --out eval/results/live.json

Costs: every box-minute and model call is recorded in the episode's
store; the totals land in the results JSON. Use --estimate to see the
plan (episodes, boxes, model calls) without spending anything.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from hindsight.eval.episode import EvalEpisode  # noqa: E402
from hindsight.eval.fork import run_forked_episode  # noqa: E402
from hindsight.eval.harness import OpenCodeAgent  # noqa: E402
from hindsight.eval.metrics import compute_metrics  # noqa: E402
from hindsight.eval.tasks import (  # noqa: E402
    Task, closing_stage, redesign_stage)

ARMS = ["baseline_alone", "baseline_redesign", "treatment",
        "treatment_relay", "treatment_diff", "full_context", "fork"]

# The 3-arm design: (1) baseline_alone = streamed
# context; (2) full_context = everything stated up front (information
# ceiling, hindsight off); (3) treatment_diff = streamed + background
# workers delivering their FULL DIFF to the main agent, which decides
# whether to incorporate. treatment/treatment_relay are the older text
# variants kept for the "what should a nudge carry?" comparison.


def _worker_health(root: Path) -> dict:
    """Sanity flag against silent fleet failures (2026-11-01
    ran 22 episodes with 42/42 explorations FAILED at driver launch — the
    episode-level scores still looked plausible). Recorded, never raised."""
    import sqlite3
    db = root / "hs" / "events.sqlite3"
    if not db.exists():  # fork arm: the treatment half roots at fork_root/
        db = root / "fork_root" / "hs" / "events.sqlite3"
    if not db.exists():
        return {}
    try:
        con = sqlite3.connect(str(db))
        rows = con.execute(
            "SELECT state, COUNT(*) FROM explorations GROUP BY state").fetchall()
        # exit_reason is the ground truth of worker execution.
        # The state machine alone lied all night (357/357 "productive" while
        # every worker's driver had exit_reason=error — the un-installable
        # `pi` harness; states marched on because the driver still wrote a
        # result.json with an empty patch).
        # '%error%' also catches 'refused: error', the exit reason the
        # collection guard writes (10 of 12 workers in relay2 expenses).
        errored = con.execute(
            "SELECT COUNT(*) FROM explorations WHERE exit_reason "
            "LIKE '%error%'").fetchone()[0]
        productive_states = ("TESTING", "GATING", "INBOX", "DISCARDED",
                             "NUDGE_READY", "SHOWN", "ADOPTED", "DELIVERED")
        marks = ",".join("?" for _ in productive_states)
        productive = con.execute(
            f"SELECT COUNT(*) FROM explorations WHERE state IN ({marks}) AND "
            "(exit_reason IS NULL OR exit_reason NOT LIKE '%error%')",
            productive_states).fetchone()[0]
    except sqlite3.Error:
        return {}
    total = sum(n for _, n in rows)
    # A worker "succeeded" if it ended cleanly and its work reached the
    # product pipeline. There is no literal SUCCEEDED state — terminal
    # states are INBOX/NUDGE_READY/DISCARDED (probe 3j: 24 productive
    # explorations flagged "broken" because all 24 were DISCARDED at rebase
    # — that is a SELECTION outcome, not a broken fleet). Proposal counts
    # are deliberately not used: errored workers saved proposals all through
    # the dead-workforce night.
    return {"explorations_total": total, "explorations_succeeded": productive,
            "explorations_errored": errored,
            "workers_broken": total > 0 and productive == 0,
            "workers_degraded": total > 0 and errored * 2 >= total}


def run_arm(arm: str, task: Task, root: Path, args) -> tuple[dict, list, list]:
    """Returns (json payload, episode results, fork outcomes). `root` must
    live OUTSIDE any real project repo: agents resolve their workspace by
    the nearest git root (found live)."""

    def agent_factory(repo_dir, script):  # live: script unused
        return OpenCodeAgent(repo_dir, model=args.model)

    repo = task.materialize(root / "repo")
    # Weak-model experiments: --worker-model flips the
    # background workforce; --reviewer-model flips detection. The judge stays
    # at its config default unless overridden via --judge-model.
    models_override = {}
    if args.worker_model:
        models_override["worker"] = args.worker_model
    if args.reviewer_model:
        models_override["reviewer"] = args.reviewer_model
    if args.judge_model:
        models_override["judge"] = args.judge_model
    settings = {"quiet_timeout": args.quiet_timeout}
    if args.stage_gap:
        settings["stage_gap_seconds"] = float(args.stage_gap)
    if arm == "fork":
        treatment, fork, note = run_forked_episode(
            task, repo, agent_factory=agent_factory, agent_script=[],
            root_dir=root / "fork_root",
            settings_overrides=settings,
            models_override=models_override or None,
            quiet_timeout=args.quiet_timeout)
        out = {"arm": arm, "treatment": _result_json(treatment),
               "fork": None, "fork_note": note,
               "worker_health": _worker_health(root)}
        forks = []
        if fork is not None:
            forks = [fork]
            out["fork"] = {
                "nudge_stage": fork.nudge_stage,
                "proposal_id": fork.proposal_id,
                "adopted": fork.adopted,
                "adopt_test_passed": fork.adopt_test_passed,
                "adopt_error": fork.adopt_error,
                "helped": fork.helped, "delta_passed": fork.delta_passed,
                "accept": _result_json(fork.accept),
                "ignore": _result_json(fork.ignore),
            }
        return out, [treatment], forks

    extra = ([redesign_stage(task)] if arm == "baseline_redesign"
             else [closing_stage()])
    agent = OpenCodeAgent(repo, model=args.model)
    episode = EvalEpisode(
        task, repo, main_agent=agent,
        hindsight=(arm in ("treatment", "treatment_relay", "treatment_diff")),
        relay_nudges=(arm in ("treatment_relay", "treatment_diff")),
        relay_diff=(arm == "treatment_diff"),
        hindsight_home=root / "hs", extra_stages=extra,
        settings_overrides=settings,
        models_override=models_override or None,
        quiet_timeout=args.quiet_timeout, arm=arm)
    try:
        result = episode.run()
    finally:
        # Saved even when the episode dies: that is when it matters most.
        (root / "main_agent_transcript.json").write_text(
            json.dumps(agent.turn_log, indent=1))
    return {"arm": arm, **_result_json(result),
            "main_agent_tokens": agent.token_totals,
            "worker_health": _worker_health(root)}, [result], []


def _result_json(r) -> dict:
    return {
        "task_id": r.task_id, "arm": r.arm, "turns": r.turns,
        "nudges": r.nudges, "runs": r.runs,
        # output tail kept: `error` is empty for test failures, and pytest's
        # own words are the only honest record of WHY
        # (A 1500-character tail cut off the earlier failures: GLM's meetings
        # list() failures were invisible, which fed a false diagnosis.)
        "final": ({"passed": r.final.passed, "failed": r.final.failed,
                   "ok": r.final.ok, "expected_tests": r.final.expected_tests,
                   "collection_errors": r.final.collection_errors,
                   "tests": r.final.tests,
                   "ran": r.final.ran, "error": r.final.error,
                   "output_tail": r.final.output or ""}
                  if r.final else None),
        "spend_usd": r.spend_usd,
        "quiet_error": r.quiet_error,
    }


def estimate(tasks: list[Task], arms: list[str]) -> None:
    """Print the plan. Honest numbers only: structure is known, spend is
    not (it depends on the agent); ranges come from the spec defaults."""
    print("Live eval plan (no money spent):")
    for t in tasks:
        stages = len(t.stages) + (1 if "baseline_redesign" in arms else 0)
        for arm in arms:
            if arm == "fork":
                eps = f"{stages} treatment + 2x remaining stages per fork"
            else:
                eps = str(stages)
            print(f"  {t.id:10s} {arm:18s} episodes~{eps}")
    print("Per design-break stage expect: 1 edge search (2 model calls),"
          " 2-3 worker boxes (20 min budget each), selection + judge.")
    print("Main agent: local opencode CLI (your provider billing).")
    print("Actuals are recorded per episode in the costs table and the"
          " results JSON.")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--tasks", nargs="+",
                    default=["eval/tasks/counters.json",
                             "eval/tasks/settings.json",
                             "eval/tasks/todos.json"])
    ap.add_argument("--arms", nargs="+", choices=ARMS, default=["fork"])
    ap.add_argument("--out", default="eval/results/live.json")
    ap.add_argument("--model", default="sail-research/zai-org/GLM-5.3",
                    help="main-agent model for opencode (provider/model)")
    # Weak-model experiments: flip individual hindsight
    # pipeline models (bare ids — provider prefix optional, harness
    # resolves via make_model_fn).
    ap.add_argument("--worker-model", default="",
                    help="background workforce model (ModelsConfig.worker)")
    ap.add_argument("--reviewer-model", default="",
                    help="design review model (ModelsConfig.reviewer)")
    ap.add_argument("--judge-model", default="",
                    help="selection/council judge model (ModelsConfig.judge)")
    ap.add_argument("--stage-gap", default="",
                    help="seconds between requests, not waiting for background "
                         "work (default: wait for it)")
    ap.add_argument("--quiet-timeout", type=float, default=3600.0)
    ap.add_argument("--parallel", type=int, default=1,
                    help="run N (task, arm) episodes concurrently (thread "
                         "pool — each episode is subprocess/IO-bound; probe-"
                         "verified that concurrent standalone "
                         "opencode CLI instances do not interfere)")
    ap.add_argument("--resume-from", default=None,
                    help="previous results JSON; completed (task, arm) pairs "
                         "are skipped and folded into the final table")
    ap.add_argument("--estimate", action="store_true",
                    help="print the plan and exit (no spend)")
    args = ap.parse_args()

    tasks = [Task.load(p) for p in args.tasks]
    if args.estimate:
        estimate(tasks, args.arms)
        return 0

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    # Episode artifacts (staged repos, stores, box checkpoints) go to a
    # temp dir OUTSIDE this project: the staged repo must not sit inside a
    # real git repo, or the eval agents adopt that repo as their workspace
    # (found live). Only the results JSON stays in the project.
    run_root = Path(tempfile.mkdtemp(prefix="hs-live-eval-"))
    print(f"run artifacts under: {run_root}", flush=True)
    results: list[dict] = []
    episode_results = []
    fork_outcomes = []

    # Resume: fold previously-completed pairs into the table and skip them.
    # A fork payload's task id lives in its nested treatment result.
    done: set[tuple[str, str]] = set()
    if args.resume_from:
        prev = json.loads(Path(args.resume_from).read_text())
        for p in prev.get("results", []):
            if p.get("arm") == "fork":
                tid = (p.get("treatment") or {}).get("task_id")
                if tid:
                    done.add((tid, "fork"))
            elif p.get("task_id"):
                done.add((p["task_id"], p["arm"]))
            results.append(p)
        print(f"resume: {len(done)} completed pairs from {args.resume_from}",
              flush=True)

    jobs = [(t, a) for t in tasks for a in args.arms if (t.id, a) not in done]

    def flush() -> None:
        # Persist incrementally: a crash mid-run must not lose episodes.
        out_path.write_text(json.dumps(
            {"started": time.strftime("%Y-%m-%d %H:%M:%S"),
             "arms": args.arms, "tasks": [t.id for t in tasks],
             "results": results}, indent=2, default=str))

    def run_one(job: tuple) -> tuple[dict, list, list]:
        task, arm = job
        root = run_root / task.id / arm / time.strftime("%Y%m%d-%H%M%S")
        print(f"== START {task.id} / {arm} -> {root}", flush=True)
        payload, eps, forks = run_arm(arm, task, root, args)
        payload["run_dir"] = str(root)
        print(f"== DONE  {task.id} / {arm}", flush=True)
        return payload, eps, forks

    if args.parallel <= 1:
        for job in jobs:
            payload, eps, forks = run_one(job)
            results.append(payload)
            episode_results.extend(eps)
            fork_outcomes.extend(forks)
            flush()
    else:
        # Result handling stays single-threaded (this thread): futures are
        # collected as they complete, so JSON writes and metric inputs never
        # race. An episode exception kills the run honestly (same as --parallel 1).
        from concurrent.futures import ThreadPoolExecutor, as_completed
        with ThreadPoolExecutor(max_workers=args.parallel) as pool:
            pending = {pool.submit(run_one, j): j for j in jobs}
            for fut in as_completed(pending):
                payload, eps, forks = fut.result()
                results.append(payload)
                episode_results.extend(eps)
                fork_outcomes.extend(forks)
                flush()

    metrics = compute_metrics(tasks, episode_results, fork_outcomes)
    payload = {"started": time.strftime("%Y-%m-%d %H:%M:%S"),
               "arms": args.arms, "tasks": [t.id for t in tasks],
               "results": results, "metrics": metrics.to_dict()}
    out_path.write_text(json.dumps(payload, indent=2, default=str))
    print(json.dumps(metrics.to_dict(), indent=2))
    print(f"written: {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
