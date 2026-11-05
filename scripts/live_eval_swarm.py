"""Swarm runner: one sailbox PER EPISODE —
each box ships the harness + a pinned opencode v2.2.5 CLI, runs
`scripts/live_eval.py` for ONE (task, arm) pair entirely in-box (nested
boxes for workers — probe-verified), and returns /root/result.json.

  uv run python scripts/live_eval_swarm.py \
      --tasks eval/tasks/v2/*.json --arms baseline_alone treatment \
      --parallel 12 --tag newcode --out eval/results/swarm/new --project .

Probe-verified 2026-11-02 (see VERIFY.md): nested sailboxes OK, in-box
opencode v2.2.5 agent turn OK, PEP 668 needs --break-system-packages.

Honesty rules (v1 carryover): a failed episode is RECORDED failed
(final.ran=false, error chain set) — never dropped. Box death
(SailboxHostLostError / SailboxTerminatedError) retries on a fresh box
up to --retries times (default 3) with backoff; every death is recorded
under box_deaths. (2026-11-02 note: the evening's "host-loss flap" was
root-caused to our own account-wide reaper killing boxes, 
— fixed store-scoped; the death-retry machinery stays as insurance
against genuine host loss.)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tarfile
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from hindsight.eval.provenance import (  # noqa: E402
    SHIPPED_PATHS, build_provenance, comparable, is_shipped)
from hindsight.sail_client import SailClient  # noqa: E402
from hindsight.sail_client import SailClientError  # noqa: E402

OC_URL = ("https://github.com/anomalyco/opencode/releases/download/"
          "v2.2.5/opencode-linux-x64.tar.gz")

SETUP = f"""
set -e
git config --global user.email eval@swarm.local
git config --global user.name 'eval swarm'
mkdir -p /root/hs /root/.config/opencode
tar -xzf /root/hs.tar.gz -C /root/hs --strip-components=1
cp /root/oc-config.json /root/.config/opencode/opencode.json
cd /root/hs && pip3 install -q --break-system-packages . pytest uv 2>&1 | tail -1
# Hidden-test scoring runs `uv run --no-project --with pytest ...` IN THE
# EPISODE BOX (isolation, see scoring._clean_env); without uv (and pytest
# for anything calling it directly) every episode records a dishonest
# fail=1 (found 2026-11-02, probes 3h/3i/3j: baselines "failed" tasks they
# pass locally; reproduced in-box as "/bin/sh: 1: uv: not found").
python3 -m pytest --version > /dev/null
uv --version > /dev/null
curl -fsSL {OC_URL} -o /root/oc.tar.gz
mkdir -p /opt/oc && tar -xzf /root/oc.tar.gz -C /opt/oc
/opt/oc/opencode --version
ln -sf /opt/oc/opencode /usr/local/bin/opencode
echo SETUP-OK
"""


def make_tarball(project: Path) -> str:
    """Ship manifest: product package + eval harness/tasks + runner."""
    path = tempfile.mktemp(suffix=".tar.gz")
    keep = lambda i: i if is_shipped(Path(i.name)) else None  # noqa: E731
    with tarfile.open(path, "w:gz") as t:
        for rel in SHIPPED_PATHS:
            t.add(project / rel, arcname=f"hs/{rel}", filter=keep)
    return path


EPISODE_TIMEOUT = 10200  # box lifetime 10800; headroom for teardown.
# Was 6900 (7200 box): the relay2 extension (4-wide parallel) lost ALL
# FOUR cells to this timeout — 4 concurrent episodes x their worker
# fleets ≈ 30 concurrent agents saturate inference and episodes run
# ~2x slower than the 2-wide probe (meetings: 6139s at parallel 2).


def cause_chain(e: BaseException) -> list[str]:
    """Exception class names down the cause/context chain. The SDK's
    SailboxHostLostError is re-wrapped as SailClientError by
    sail_client.run, so the informative name is only in the chain."""
    names, seen, cur = [], set(), e
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        names.append(type(cur).__name__)
        cur = cur.__cause__ or cur.__context__
    return names


# Two faces of the same platform death (2026-11-02 flap: 3/3 long-lived
# boxes died in one evening vs 1 loss in 9 daytime hours): the exec channel
# dies -> SailboxHostLostError; a later call finds the box GC'd ->
# SailboxTerminatedError. Both mean "start over on a fresh box". During an
# episode a Terminated can only be external: our own terminate happens in
# `finally` after recording, and the box lifetime (7200s) exceeds the
# episode timeout (6900s).
def is_box_death(chain: list[str], message: str) -> bool:
    joined = "".join(chain)
    return ("HostLost" in joined or "SailboxTerminatedError" in joined
            or "sailbox was lost" in message or "no longer running" in message)


def run_episode_backgrounded(sail: SailClient, box_id: str, cmd: str,
                             env: dict, timeout: int) -> float:
    """Fire-and-forget exec + short polls. A multi-hour SYNCHRONOUS
    sail.run died on every attempt (2026-11-02 probe: 2/2 HostLost); the
    remote PROCESS survives an exec-channel drop, so backgrounding plus
    45s polls tolerates drops, and the on-box log gives post-mortems."""
    full = (f"{cmd} > /root/episode.log 2>&1; "
            "echo $? > /root/episode.exit; touch /root/episode.done")
    sail.start_background(box_id, ["bash", "-lc", full], env=env)
    t0 = time.time()
    consecutive_errors = 0
    while True:
        if time.time() - t0 > timeout:
            raise TimeoutError(f"episode exceeded {timeout}s in-box")
        time.sleep(45)
        try:
            probe = sail.run(
                box_id,
                ["bash", "-c", "test -f /root/episode.done "
                                "&& echo DONE || echo RUNNING"],
                timeout=60)
            consecutive_errors = 0
        except Exception as e:  # noqa: BLE001
            if is_box_death(cause_chain(e), str(e)):
                raise  # box is gone — the fresh-box retry handles it
            consecutive_errors += 1
            if consecutive_errors >= 3:
                raise
            continue
        if (probe.stdout or "").strip() == "DONE":
            break
    wall_s = round(time.time() - t0)
    out = sail.run(box_id, ["bash", "-c", "cat /root/episode.exit"], timeout=30)
    exit_code = (out.stdout or "").strip()
    if exit_code != "0":
        tail = sail.run(box_id, ["bash", "-c", "tail -c 1500 /root/episode.log"],
                        timeout=30)
        raise SailClientError(
            f"episode exited {exit_code}: {(tail.stdout or '')[-1000:]}")
    return wall_s


def pull_artifacts(sail: SailClient, box_id: str, artifacts_dir: Path,
                   job_key: str) -> None:
    """Fetch the diagnosable record BEFORE the box dies: episode log and a
    tar of the run dirs (repo + hindsight store). A failed episode whose
    artifacts vanished with the box is undiagnosable (probe 3h, first
    landing: three fail=1 with zero explanation available)."""
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    try:
        sail.download_file(box_id, "/root/episode.log",
                           str(artifacts_dir / f"{job_key}.log"))
    except Exception:  # noqa: BLE001
        pass
    try:
        sail.run(box_id, ["bash", "-c",
                          "tar -czf /root/rundir.tar.gz -C /tmp "
                          "$(ls -d /tmp/hs-live-eval-* 2>/dev/null | head -1) "
                          "2>/dev/null; test -s /root/rundir.tar.gz"],
                 timeout=120)
        sail.download_file(box_id, "/root/rundir.tar.gz",
                           str(artifacts_dir / f"{job_key}.rundir.tar.gz"))
    except Exception:  # noqa: BLE001
        pass


def job_key_of(job: dict) -> str:
    """Unique per (task, arm, repetition) — a 36-episode table is
    9 tasks x 2 arms x 2 reps (rep suffix added 2026-11-02)."""
    return f"{job['task_id']}__{job['arm']}__r{job.get('rep', 0)}"


def run_job(sail: SailClient, job: dict, tarball: str, config_path: Path,
            quiet_timeout: float, model: str,
            artifacts_dir: Path | None = None) -> dict:
    """One episode in one fresh box. Never raises: failures are recorded."""
    arm, tag = job["arm"], job["tag"]
    job_key = job_key_of(job)
    episode = {"task_id": job["task_id"], "arm": arm, "turns": 0,
               "nudges": [], "runs": [], "final": None, 
               "spend_usd": 0.0, "quiet_error": "", "swarm": True}
    box = None
    try:
        box = sail.create_box(
            f"hs-swarm-{tag}-{job['task_id']}-{arm}-r{job.get('rep', 0)}"[:56],
            image=None, max_lifetime_seconds=10800)
        sail.upload_file(box.id, tarball, "/root/hs.tar.gz")
        sail.upload_file(box.id, str(config_path), "/root/oc-config.json")
        out = sail.run(box.id, ["bash", "-lc", SETUP], timeout=900)
        if "SETUP-OK" not in (out.stdout or ""):
            raise SailClientError(
                f"setup failed: {(out.stdout or '')[-300:]} "
                f"{(out.stderr or '')[-300:]}")
        # The key goes via env, NOT the command line: an exception message
        # quotes the command and would leak it into recorded results JSON.
        cmd = (f"cd /root/hs && "
               f"python3 scripts/live_eval.py --tasks {job['rel_task']} "
               f"--arms {arm} --out /root/result.json --model {model} "
               f"--quiet-timeout {quiet_timeout}")
        # Weak-model experiments: model ids are not secrets.
        for flag in ("worker_model", "reviewer_model", "judge_model", "stage_gap"):
            if job.get(flag):
                cmd += f" --{flag.replace('_', '-')} {job[flag]}"
        wall_s = run_episode_backgrounded(
            sail, box.id, cmd,
            env={"SAIL_API_KEY": os.environ.get("SAIL_API_KEY", "")},
            timeout=EPISODE_TIMEOUT)
        local_tmp = Path(tempfile.mktemp(suffix=".json"))
        sail.download_file(box.id, "/root/result.json", str(local_tmp))
        episode = json.loads(local_tmp.read_text())["results"][0]
        episode["swarm"] = True
        episode["wall_s"] = wall_s
        return episode
    except Exception as e:  # noqa: BLE001 - recorded, never dropped
        chain = cause_chain(e)
        # Teardown-crash recovery: the in-box episode can
        # SIGABRT (exit 134) AFTER writing a complete result.json — twice
        # tonight, both expenses/treatment_relay, two different models.
        # The work is done; recover the payload instead of discarding
        # it. The crash is recorded in `teardown_crash`, never hidden.
        if box is not None:
            try:
                local_tmp = Path(tempfile.mktemp(suffix=".json"))
                sail.download_file(box.id, "/root/result.json", str(local_tmp))
                recovered = json.loads(local_tmp.read_text())["results"][0]
                if (recovered.get("final") or {}).get("ran"):
                    recovered["swarm"] = True
                    recovered["teardown_crash"] = f"{type(e).__name__}: {e}"[:200]
                    return recovered
            except Exception:  # noqa: BLE001 - nothing valid to recover
                pass
        episode["final"] = {"passed": 0, "failed": 0, "ran": False,
                            "error": f"{type(e).__name__}: {e}"[:400],
                            "root_cause": chain[-1] if chain else "",
                            "error_chain": chain}
        if box is not None:  # post-mortem: what was the episode doing?
            try:
                tail = sail.run(box.id, ["bash", "-c",
                                         "tail -c 2000 /root/episode.log "
                                         "2>/dev/null || echo NO-LOG"],
                                timeout=30)
                episode["debug_tail"] = (tail.stdout or "")[-2000:]
            except Exception:  # noqa: BLE001 - box may be gone
                pass
        episode["quiet_error"] = "swarm episode failed (recorded)"
        return episode
    finally:
        if box is not None:
            if artifacts_dir is not None:
                pull_artifacts(sail, box.id, artifacts_dir, job_key)
            try:
                sail.terminate(box.id)
            except Exception:  # noqa: BLE001
                pass


def run_boxed(sail: SailClient, job: dict, tarball: str, config_path: Path,
              quiet_timeout: float, model: str, retries: int = 3,
              artifacts_dir: Path | None = None) -> dict:
    """Box death (host loss / box GC) is the one retryable failure: fresh
    box, up to `retries` attempts, brief backoff. Anything else records
    failed immediately — retries never mask logic failures."""
    attempts = []
    for attempt in range(1, retries + 1):
        ep = run_job(sail, job, tarball, config_path, quiet_timeout, model,
                     artifacts_dir)
        final = ep.get("final") or {}
        if final.get("ran") or not is_box_death(
                (final.get("error_chain")) or [], final.get("error") or ""):
            break
        attempts.append({"attempt": attempt,
                         "root_cause": final.get("root_cause"),
                         "error": (final.get("error") or "")[:200]})
        time.sleep(20 * attempt)  # let the flap pass; fresh box next
    if attempts:
        ep["box_deaths"] = attempts
        ep["attempts"] = len(attempts) + 1
    return ep


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--tasks", nargs="+", required=True)
    ap.add_argument("--arms", nargs="+",
                    choices=["baseline_alone", "baseline_redesign",
                             "treatment", "treatment_relay",
                             "treatment_diff", "full_context", "fork"],
                    default=["baseline_alone", "baseline_redesign",
                             "treatment", "fork"])
    ap.add_argument("--parallel", type=int, default=12,
                    help="boxes in flight at once")
    ap.add_argument("--tag", required=True,
                    help="run tag (box names + result dir)")
    ap.add_argument("--project", default=".",
                    help="project tree to ship (main tree or a pinned "
                         "worktree for policy comparisons)")
    ap.add_argument("--out", required=True, help="result dir (per-job JSONs "
                        "land incrementally; table.json merged at the end)")
    # Same default as scripts/live_eval.py (3600s): the swarm must not
    # change episode semantics vs the sequential runner.
    ap.add_argument("--quiet-timeout", type=float, default=3600.0)
    ap.add_argument("--model", default="sail-research/zai-org/GLM-5.3")
    ap.add_argument("--real-key-in-box", action="store_true",
                    help="required: this runner puts the real SAIL_API_KEY in each episode's "
                         "box, where its agents can read it. Prefer evocode_pilot.py")
    ap.add_argument("--repeats", type=int, default=1,
                    help="episodes per task+arm (36-episode table = 9 tasks "
                         "x 2 arms x --repeats 2)")
    # Weak-model experiments: flip hindsight pipeline models
    # inside every episode (main agent via --model as before).
    ap.add_argument("--worker-model", default="",
                    help="background workforce model (ModelsConfig.worker)")
    ap.add_argument("--reviewer-model", default="",
                    help="design review model (ModelsConfig.reviewer)")
    ap.add_argument("--judge-model", default="",
                    help="selection/council judge model (ModelsConfig.judge)")
    ap.add_argument("--stage-gap", default="",
                    help="seconds between requests, not waiting for background "
                         "work (default: wait for it)")
    ap.add_argument("--retries", type=int, default=3,
                    help="attempts per episode when the BOX dies "
                         "(host loss / box GC); logic failures never retry")
    ap.add_argument("--allow-mixed-vintage", action="store_true",
                    help="resume into a dir whose landed episodes came from "
                         "different code, tasks or models (labelled, not "
                         "comparable)")
    args = ap.parse_args()
    if not args.real_key_in_box:
        ap.error("this runner puts the real key in a box an agent can read; "
                 "pass --real-key-in-box to accept that")

    project = Path(args.project).resolve()
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    config_path = Path.home() / ".config/opencode/opencode.json"
    tarball = make_tarball(project)
    print(f"tarball: {Path(tarball).stat().st_size // 1024} KB from {project}",
          flush=True)
    provenance = build_provenance(
        project, model=args.model, worker_model=args.worker_model,
        reviewer_model=args.reviewer_model, judge_model=args.judge_model,
        stage_gap=args.stage_gap)
    print(f"provenance: {provenance['git_sha'][:10]}"
          f"{' (dirty)' if provenance['git_dirty'] else ''} "
          f"content {provenance['content_sha256'][:12]}", flush=True)
    # Resume skips by filename, so a landed file from older code would
    # otherwise join this table silently.
    stale = [p.name for p in sorted(out_dir.glob("*__*.json"))
             if not comparable(json.loads(p.read_text()).get("provenance"),
                               provenance)]
    if stale and not args.allow_mixed_vintage:
        print(f"REFUSING to resume: {len(stale)} landed episode(s) in {out_dir} "
              f"come from different code, tasks or models: {stale[:5]}. Use a "
              "new --out dir, or pass --allow-mixed-vintage.", flush=True)
        return 2

    sail = SailClient()
    jobs = []
    for task_file in args.tasks:
        tid = Path(task_file).stem
        # Task path as it will exist INSIDE the box, relative to /root/hs.
        # Absolute paths under the shipped project tree, or relative paths
        # from the CWD, both resolve against the project root.
        p = Path(task_file)
        abs_p = p if p.is_absolute() else (Path.cwd() / p)
        rel_task = str(abs_p.resolve().relative_to(project)) \
            if abs_p.resolve().is_relative_to(project) else str(p)
        for arm in args.arms:
            for rep in range(args.repeats):
                job = {"rel_task": rel_task, "task_id": tid,
                       "arm": arm, "tag": args.tag, "rep": rep}
                # Weak-model experiments
                for flag in ("worker_model", "reviewer_model",
                             "judge_model", "stage_gap"):
                    v = getattr(args, flag, None)
                    if v:
                        job[flag] = v
                job_file = out_dir / f"{job_key_of(job)}.json"
                if job_file.exists():  # resume: completed job files are kept
                    continue
                jobs.append(job)
    print(f"jobs: {len(jobs)} ({args.parallel} in flight)", flush=True)
    if not jobs:
        print("nothing to do (all job files present)")

    done = 0
    artifacts_dir = out_dir / "artifacts"
    with ThreadPoolExecutor(max_workers=args.parallel) as pool:
        futs = {pool.submit(run_boxed, sail, j, tarball, config_path,
                            args.quiet_timeout, args.model,
                            args.retries, artifacts_dir): j for j in jobs}
        for fut in as_completed(futs):
            j = futs[fut]
            ep = fut.result()
            ep["rep"] = j.get("rep", 0)
            ep["provenance"] = provenance
            (out_dir / f"{job_key_of(j)}.json").write_text(
                json.dumps(ep, indent=2, default=str))
            done += 1
            final = ep.get("final") or {}
            print(f"[{done}/{len(jobs)}] {j['task_id']}/{j['arm']}: "
                  f"pass={final.get('passed')} fail={final.get('failed')} "
                  f"ran={final.get('ran')} ${ep.get('spend_usd', 0):.2f}",
                  flush=True)

    # Merged table over ALL job files (including resumed ones).
    table = [json.loads(p.read_text()) for p in sorted(out_dir.glob("*__*.json"))]
    spend = sum((e.get("spend_usd") or 0) for e in table)
    (out_dir / "table.json").write_text(json.dumps(
        {"tag": args.tag, "project": str(project), "parallel": args.parallel,
         "provenance": provenance,
         "finished": time.strftime("%Y-%m-%d %H:%M:%S"),
         "results": table, "spend_usd_total": spend}, indent=2, default=str))
    print(f"written: {out_dir/'table.json'} (spend ${spend:.2f})", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
