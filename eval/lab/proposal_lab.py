"""Which worker instruction produces patches that fix graded failures?

See PREREG.md. One sailbox per (checkpoint, arm, rep). Hidden tests reach the box
only after the worker has finished, so they cannot leak to it.

  uv run python eval/lab/proposal_lab.py --from-dir eval/results/swarm/calib1 \
      --tag lab1 --reps 2 --parallel 16 [--dry-run]
  uv run python eval/lab/proposal_lab.py --report eval/results/lab/lab1
"""

from __future__ import annotations

import argparse
import glob
import io
import json
import os
import shutil
import sys
import tarfile
import tempfile
import time
import xml.etree.ElementTree as ET
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from math import comb
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from hindsight.eval.harness import _scan_events  # noqa: E402
from hindsight.eval.provenance import git_state  # noqa: E402
from hindsight.sail_client import SailClient  # noqa: E402
from live_eval_swarm import OC_URL, run_episode_backgrounded  # noqa: E402

MODEL = "sail-research/zai-org/GLM-5.3"
WORKER_TIMEOUT = 1800
HIDDEN = ".hindsight_eval_hidden"

SETUP = f"""
set -e
git config --global user.email lab@swarm.local
git config --global user.name lab
mkdir -p /root/.config/opencode /root/repo
cp /root/oc-config.json /root/.config/opencode/opencode.json
tar -xzf /root/repo.tar.gz -C /root/repo
pip3 install -q --break-system-packages pytest 2>&1 | tail -1
curl -fsSL {OC_URL} -o /root/oc.tar.gz
mkdir -p /opt/oc && tar -xzf /root/oc.tar.gz -C /opt/oc
ln -sf /opt/oc/opencode /usr/local/bin/opencode
cp -r /root/repo /root/base
cd /root/repo && git init -q && git add -A && git commit -qm base --allow-empty
echo SETUP-OK
"""

# `git add` with a pathspec naming an ignored path exits 1, so never chain on it
# or filter there: lab1 and lab2 lost patches in every repo with a .gitignore.
CAPTURE = ("cd /root/repo; git add -A; git diff --cached "
           "$(git rev-list --max-parents=0 HEAD) -- . "
           "':(exclude)*.pyc' ':(exclude)*__pycache__*' ':(exclude).pytest_cache'")
CHANGED = ("diff -rq -x .git -x __pycache__ -x .pytest_cache /root/base /root/repo | "
           "sed -E 's#^Files /root/base/([^ ]+) and .*#\\1#; "
           "s#^Only in /root/(base|repo)/?([^:]*): (.*)#\\2/\\3#'")

COMMON = """You are a background engineer. A user has been working with a coding agent;
the code in this directory is the result. You are not talking to the user.

Here is everything the user asked for, verbatim, in order:

{requests}

Rules:
- If the user did not ask for it, do not require it. Do not invent requirements.
- Keep the public API the user described.
- Run the tests before finishing: python3 -m pytest -q
"""

ARMS = {
    "reqs_audit": COMMON + """
Your job: go through the requests one at a time. For each, check precisely what the
code does against what was asked, including how the requests interact, and write a
test for anything not yet covered. Fix every mismatch you find. If the code already
does everything asked, change nothing.
""",
    "edge_hunt": COMMON + """
Your job: find up to 5 edge cases where the current design fails, write each as a
failing test under hs_edge/, then redesign the code so that they all pass. Run them
too: python3 -m pytest -q hs_edge
""",
    "fresh_rebuild": COMMON + """
Your job: the directory has been emptied. Design and build the complete solution
from scratch, knowing everything above up front, with its own tests.
""",
}


def requests_text(task: dict) -> str:
    return "\n\n".join(f"Request {i + 1}: {s['message']}"
                       for i, s in enumerate(task["stages"]))


def hidden_tests(task: dict) -> dict[str, str]:
    out: dict[str, str] = {}
    for stage in task["stages"]:
        out.update(stage.get("hidden_tests") or {})
    return out


def find_checkpoints(from_dir: Path, tasks_dir: Path, work: Path) -> list[dict]:
    """Final repos of baseline episodes, as tarballs ready to upload."""
    checkpoints = []
    for ep_path in sorted(from_dir.glob("*__baseline_alone__*.json")):
        ep = json.loads(ep_path.read_text())
        final = ep.get("final") or {}
        tar_path = from_dir / "artifacts" / f"{ep_path.stem}.rundir.tar.gz"
        if not final.get("ran") or not tar_path.exists():
            continue
        task_id = ep_path.stem.split("__")[0]
        task_path = tasks_dir / f"{task_id}.json"
        if not task_path.exists():
            continue
        dest = work / ep_path.stem
        dest.mkdir()
        with tarfile.open(tar_path) as t:
            t.extractall(dest, filter="data")
        repos = [p for p in dest.rglob("repo")
                 if p.is_dir() and (p / ".git").exists() and "fork" not in p.parts]
        if not repos:
            continue
        repo_tar = work / f"{ep_path.stem}.repo.tar.gz"
        with tarfile.open(repo_tar, "w:gz") as t:
            for f in repos[0].iterdir():
                if f.name in (".git", "__pycache__", ".pytest_cache"):
                    continue
                t.add(f, arcname=f.name, filter=lambda i: (
                    None if "__pycache__" in i.name or i.name.endswith(".pyc")
                    else i))
        checkpoints.append({
            "id": ep_path.stem, "task_id": task_id, "repo_tar": str(repo_tar),
            "failing": not final.get("ok", final.get("failed", 1) == 0)})
    return checkpoints


def score(sail: SailClient, box_id: str, repo: str, tests: dict[str, str]) -> dict:
    sail.write_files(box_id, {f"{repo}/{HIDDEN}/{n}": c for n, c in tests.items()})
    report = f"/root/junit-{os.path.basename(repo)}.xml"
    sail.run(box_id, ["bash", "-c",
                      f"cd {repo} && timeout 120 python3 -m pytest -q {HIDDEN} "
                      f"--junitxml={report} -p no:cacheprovider > /dev/null 2>&1; "
                      f"rm -rf {HIDDEN}; true"], timeout=180)
    cases = list(ET.fromstring(sail.read_file(box_id, report)).iter("testcase"))
    bad = {c.get("name"): ([el.get("message") for el in c
                            if el.tag in ("failure", "error")] or [""])[0][:200]
           for c in cases if any(el.tag in ("failure", "error") for el in c)}
    return {"passed": len(cases) - len(bad), "failed": len(bad), "failures": bad}


def run_job(sail: SailClient, job: dict, config_path: Path) -> dict:
    result = {**{k: job[k] for k in ("checkpoint", "task_id", "arm", "rep", "failing")}}
    box = None
    t0 = time.time()
    try:
        box = sail.create_box(
            f"hs-lab-{job['tag']}-{job['arm']}-{job['rep']}-{job['checkpoint']}"[:56],
            image=None, max_lifetime_seconds=WORKER_TIMEOUT + 900)
        sail.upload_file(box.id, job["repo_tar"], "/root/repo.tar.gz")
        sail.upload_file(box.id, str(config_path), "/root/oc-config.json")
        out = sail.run(box.id, ["bash", "-lc", SETUP], timeout=600)
        if "SETUP-OK" not in (out.stdout or ""):
            raise RuntimeError(f"setup failed: {(out.stderr or '')[-300:]}")
        if job["arm"] == "fresh_rebuild":
            sail.run(box.id, ["bash", "-c",
                              "cd /root/repo && git rm -rq --ignore-unmatch . && "
                              "git commit -qm emptied --allow-empty"], timeout=60)
        task = json.loads(Path(job["task_path"]).read_text())
        prompt = ARMS[job["arm"]].format(requests=requests_text(task))
        sail.write_files(box.id, {"/root/prompt.txt": prompt})
        session = f"ses-lab-{job['rep']}{abs(hash(job['checkpoint'])) % 10 ** 8}"
        cmd = (f"cd /root/repo && PWD=/root/repo opencode run --standalone "
               f"--session {session} --auto --format json --model {MODEL} "
               '"$(cat /root/prompt.txt)"')
        result["wall_s"] = run_episode_backgrounded(
            sail, box.id, cmd, env={}, timeout=WORKER_TIMEOUT)
        log = sail.read_file(box.id, "/root/episode.log").decode(errors="replace")
        errors, tokens = _scan_events(log)
        result["tokens"] = tokens
        result["agent_errors"] = errors[-2:]
        # Everything below sees hidden tests; the worker has already finished.
        tests = hidden_tests(task)
        result["base"] = score(sail, box.id, "/root/base", tests)
        result["after"] = score(sail, box.id, "/root/repo", tests)
        own = sail.run(box.id, ["bash", "-c",
                                "cd /root/repo && timeout 120 python3 -m pytest -q "
                                "2>&1 | tail -1"], timeout=180)
        result["own_suite"] = (own.stdout or "").strip()[:120]
        diff = sail.run(box.id, ["bash", "-c", CAPTURE], timeout=60)
        result["patch"] = diff.stdout or ""
        # Independent of git, so a broken capture cannot pass as "no change".
        changed = sail.run(box.id, ["bash", "-c", CHANGED], timeout=60)
        result["changed_files"] = (changed.stdout or "").split()
        if result["changed_files"] and not result["patch"]:
            raise RuntimeError(f"capture lost changes: {result['changed_files']}")
        result["ran"] = True
    except Exception as e:  # noqa: BLE001 - recorded, never dropped
        result["ran"] = False
        result["error"] = f"{type(e).__name__}: {e}"[:400]
    finally:
        result["total_s"] = round(time.time() - t0)
        if box is not None:
            try:
                sail.terminate(box.id)
            except Exception:  # noqa: BLE001
                pass
    return result


def launch(args: argparse.Namespace) -> int:
    out = ROOT / "eval/results/lab" / args.tag
    out.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="hs-lab-"))
    tasks_dir = Path(args.tasks_dir).resolve()
    checkpoints = find_checkpoints(Path(args.from_dir), tasks_dir, work)
    failing = [c for c in checkpoints if c["failing"]]
    passing = [c for c in checkpoints if not c["failing"]]
    # Failing checkpoints are the experiment; passing ones only measure harm.
    by_task = defaultdict(list)
    for c in passing:
        by_task[c["task_id"]].append(c)
    chosen = failing + [c for cs in by_task.values() for c in cs[:args.passing_per_task]]
    print(f"checkpoints: {len(failing)} failing, {len(passing)} passing, "
          f"{len(chosen)} chosen; by task: "
          f"{dict((t, sum(c['task_id'] == t for c in failing)) for t in sorted({c['task_id'] for c in failing}))}",
          flush=True)
    jobs = []
    for c in chosen:
        for arm in args.arms:
            for rep in range(args.reps):
                job = {"checkpoint": c["id"], "task_id": c["task_id"], "arm": arm,
                       "rep": rep, "tag": args.tag, "failing": c["failing"],
                       "repo_tar": c["repo_tar"],
                       "task_path": str(tasks_dir / f"{c['task_id']}.json")}
                if not (out / f"{c['id']}__{arm}__{rep}.json").exists():
                    jobs.append(job)
    print(f"jobs: {len(jobs)} ({args.parallel} in flight)", flush=True)
    if args.dry_run or not jobs:
        shutil.rmtree(work, ignore_errors=True)
        return 0
    (out / "manifest.json").write_text(json.dumps(
        {**git_state(ROOT), "model": MODEL, "arms": ARMS}, indent=1))
    sail = SailClient()
    config = Path.home() / ".config/opencode/opencode.json"
    with ThreadPoolExecutor(max_workers=args.parallel) as pool:
        futures = {pool.submit(run_job, sail, j, config): j for j in jobs}
        for i, fut in enumerate(as_completed(futures), 1):
            j = futures[fut]
            r = fut.result()
            (out / f"{j['checkpoint']}__{j['arm']}__{j['rep']}.json").write_text(
                json.dumps(r, indent=1))
            print(f"[{i}/{len(jobs)}] {j['checkpoint']} {j['arm']} ran={r.get('ran')} "
                  f"{(r.get('base') or {}).get('passed')}->"
                  f"{(r.get('after') or {}).get('passed')} {r.get('error', '')[:80]}",
                  flush=True)
    shutil.rmtree(work, ignore_errors=True)
    return 0


def sign_test(wins: int, losses: int) -> float:
    n = wins + losses
    if not n:
        return 1.0
    tail = sum(comb(n, k) for k in range(max(wins, losses), n + 1)) / 2 ** n
    return min(1.0, 2 * tail)


def report(result_dir: Path) -> int:
    rows = [json.loads(Path(p).read_text())
            for p in sorted(glob.glob(str(result_dir / "*__*__*.json")))]
    good = [r for r in rows if r.get("ran") and not r.get("agent_errors")]
    print(f"runs {len(rows)}, usable {len(good)}")
    for r in rows:
        if r not in good:
            print(f"  unusable: {r['checkpoint']} {r['arm']} "
                  f"{(r.get('error') or r.get('agent_errors'))}"[:160])

    def fixed(r: dict) -> bool:
        return r["after"]["failed"] == 0 and r["after"]["passed"] > 0

    arms = sorted({r["arm"] for r in good})
    print("\nfailing checkpoints:")
    for arm in arms:
        rs = [r for r in good if r["arm"] == arm and r["failing"]]
        if rs:
            better = sum(r["after"]["passed"] > r["base"]["passed"] for r in rs)
            worse = sum(r["after"]["passed"] < r["base"]["passed"] for r in rs)
            print(f"  {arm:<16} rescued {sum(map(fixed, rs))}/{len(rs)} "
                  f"({sum(map(fixed, rs)) / len(rs):.0%}), improved {better}, "
                  f"worse {worse}")
    print("\nby task (rescued/runs):")
    for t in sorted({r["task_id"] for r in good if r["failing"]}):
        cells = []
        for arm in arms:
            rs = [r for r in good if r["arm"] == arm and r["failing"]
                  and r["task_id"] == t]
            cells.append(f"{arm} {sum(map(fixed, rs))}/{len(rs)}")
        print(f"  {t:<12} " + "  ".join(cells))
    print("\npassing checkpoints (harm = no longer passes):")
    for arm in arms:
        rs = [r for r in good if r["arm"] == arm and not r["failing"]]
        if rs:
            print(f"  {arm:<16} harmed {sum(not fixed(r) for r in rs)}/{len(rs)}")

    if {"reqs_audit", "edge_hunt"} <= set(arms):
        wins = losses = 0
        by_cp = defaultdict(lambda: defaultdict(list))
        for r in good:
            if r["failing"]:
                by_cp[r["checkpoint"]][r["arm"]].append(fixed(r))
        for cp in by_cp.values():
            if cp["reqs_audit"] and cp["edge_hunt"]:
                a = sum(cp["reqs_audit"]) / len(cp["reqs_audit"])
                b = sum(cp["edge_hunt"]) / len(cp["edge_hunt"])
                wins += a > b
                losses += a < b
        print(f"\nreqs_audit vs edge_hunt by checkpoint: {wins} wins, {losses} "
              f"losses, sign test p={sign_test(wins, losses):.3f}")
    used = defaultdict(list)
    for r in good:
        used[r["arm"]].append(sum((r.get("tokens") or {}).values()))
    print("\nmean tokens / wall seconds:")
    for arm in arms:
        walls = [r.get("wall_s") or 0 for r in good if r["arm"] == arm]
        print(f"  {arm:<16} {sum(used[arm]) // len(used[arm])} / "
              f"{sum(walls) // len(walls)}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--from-dir", default="eval/results/swarm/calib1")
    ap.add_argument("--tasks-dir", default="eval/tasks/v2")
    ap.add_argument("--tag", default="lab1")
    ap.add_argument("--arms", nargs="+", choices=list(ARMS), default=list(ARMS))
    ap.add_argument("--reps", type=int, default=2)
    ap.add_argument("--passing-per-task", type=int, default=1)
    ap.add_argument("--parallel", type=int, default=16)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--report", default="")
    args = ap.parse_args()
    if args.report:
        return report(Path(args.report))
    return launch(args)


if __name__ == "__main__":
    raise SystemExit(main())
