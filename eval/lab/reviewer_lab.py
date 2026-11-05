"""Can a read-only reviewer tell failing repos from passing ones? See PREREG_reviewer.md.

  uv run python eval/lab/reviewer_lab.py --tag rev1 [--dry-run]
  uv run python eval/lab/reviewer_lab.py --report eval/results/lab/rev1
"""

from __future__ import annotations

import argparse
import glob
import json
import sys
import tarfile
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from hindsight.sail_client import SailClient  # noqa: E402

MODELS = {"flash": "zai-org/GLM-5.3-Flash", "kimi": "moonshotai/Kimi-K3"}

DEFINITIONS = {
    "broad": ("Answer yes if the code does not do what the user asked, including how "
              "their requests interact, or if the latest request means an earlier "
              "decision in the code should change."),
    "strict": ("Answer yes only if there is a design-level problem: the structure of "
               "the code is wrong for what the user asked, and a local fix would not "
               "solve it."),
}

PROMPT = """A user has been working with a coding agent. Decide whether background
engineers should be called in to rework this code.

{definition}
Otherwise answer no. If the user did not ask for it, do not require it.

Focus on the latest request, in light of the earlier ones.

EARLIER REQUESTS:
{earlier}

LATEST REQUEST:
{latest}

THE CODE:
{code}

Answer with JSON only: {{"call_background": true or false, "reason": "one sentence"}}
"""


def load_code(tar_path: Path) -> str:
    parts = []
    with tarfile.open(tar_path) as t:
        for m in t.getmembers():
            segs = m.name.split("/")
            if not m.isfile() or "repo" not in segs or "fork" in segs:
                continue
            rel = segs[segs.index("repo") + 1:]
            if not rel or rel[0] == ".git" or "__pycache__" in rel:
                continue
            if rel[0].startswith(".pytest") or rel[-1].endswith(".pyc"):
                continue
            # Never show grading material, even if an episode left some behind.
            if any("hidden" in s.lower() for s in rel):
                raise RuntimeError(f"hidden test material in {tar_path}: {rel}")
            raw = t.extractfile(m).read()
            if b"\0" in raw[:4096]:
                continue
            parts.append(f"--- {'/'.join(rel)} ---\n{raw.decode(errors='replace')}")
    return "\n\n".join(sorted(parts))


def repos(from_dir: Path, tasks_dir: Path) -> list[dict]:
    out = []
    for p in sorted(from_dir.glob("*__baseline_alone__*.json")):
        final = json.loads(p.read_text()).get("final") or {}
        tar = from_dir / "artifacts" / f"{p.stem}.rundir.tar.gz"
        task_id = p.stem.split("__")[0]
        task_path = tasks_dir / f"{task_id}.json"
        if not final.get("ran") or not tar.exists() or not task_path.exists():
            continue
        msgs = [s["message"] for s in json.loads(task_path.read_text())["stages"]]
        out.append({"id": p.stem, "task": task_id, "failing": not final.get("ok"),
                    "earlier": "\n\n".join(msgs[:-1]) or "(none)", "latest": msgs[-1],
                    "code": load_code(tar)})
    return out


def review(sail: SailClient, repo: dict, definition: str, model: str, rep: int) -> dict:
    row = {"repo": repo["id"], "task": repo["task"], "failing": repo["failing"],
           "definition": definition, "model": model, "rep": rep}
    prompt = PROMPT.format(definition=DEFINITIONS[definition], earlier=repo["earlier"],
                           latest=repo["latest"], code=repo["code"])
    t0 = time.time()
    for attempt in range(4):
        try:
            resp = sail.inference_chat(
                MODELS[model], [{"role": "user", "content": prompt}], timeout=180,
                max_output_tokens=16000)
            content = resp["choices"][0]["message"].get("content")
            raw = (content if isinstance(content, str) else
                   "".join(c.get("text", "") for c in content or []))
            data = json.loads(raw[raw.find("{"):raw.rfind("}") + 1])
            if not isinstance(data.get("call_background"), bool):
                raise ValueError("no boolean verdict")
            row.update(yes=data["call_background"], reason=str(data.get("reason"))[:300])
            row["usage"] = (resp.get("usage") or {}).get("total_tokens")
            break
        except Exception as e:  # noqa: BLE001 - recorded, never dropped
            row["error"] = f"{type(e).__name__}: {e}"[:300]
            time.sleep(5 * (attempt + 1))
    else:
        row["yes"] = None
    if row.get("yes") is not None:
        row.pop("error", None)
    row["seconds"] = round(time.time() - t0, 1)
    return row


def rate(rows: list[dict]) -> str:
    good = [r for r in rows if r["yes"] is not None]
    if not good:
        return "n/a"
    k = sum(r["yes"] for r in good)
    return f"{k}/{len(good)} ({k / len(good):.0%})"


def report(out: Path) -> int:
    rows = json.loads((out / "rows.json").read_text())
    print(f"reviews {len(rows)}, no verdict {sum(r['yes'] is None for r in rows)}")
    fail_tasks = {r["task"] for r in rows if r["failing"]}
    for model in MODELS:
        for d in DEFINITIONS:
            rs = [r for r in rows if r["model"] == model and r["definition"] == d]
            if not rs:
                continue
            f = [r for r in rs if r["failing"]]
            same = [r for r in rs if not r["failing"] and r["task"] in fail_tasks]
            other = [r for r in rs if not r["failing"] and r["task"] not in fail_tasks]
            secs = sorted(r["seconds"] for r in rs)
            print(f"\n{model} / {d}: caught {rate(f)}; false yes, same tasks "
                  f"{rate(same)}; other tasks {rate(other)}; median "
                  f"{secs[len(secs) // 2]}s, max {secs[-1]}s")
            by = defaultdict(list)
            for r in rs:
                by[(r["task"], r["failing"])].append(r)
            for (t, fl), group in sorted(by.items()):
                print(f"  {t:<10} {'failing' if fl else 'passing'}  {rate(group)}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", default="rev1")
    ap.add_argument("--reps", type=int, default=3)
    ap.add_argument("--models", nargs="+", choices=list(MODELS), default=list(MODELS))
    ap.add_argument("--parallel", type=int, default=16)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--report", default="")
    args = ap.parse_args()
    if args.report:
        return report(Path(args.report))
    all_repos = repos(ROOT / "eval/results/swarm/calib1", ROOT / "eval/tasks/v2")
    print(f"repos {len(all_repos)}, failing {sum(r['failing'] for r in all_repos)}, "
          f"largest {max(len(r['code']) for r in all_repos)} chars", flush=True)
    jobs = [(r, d, m, n) for r in all_repos for d in DEFINITIONS for m in args.models
            for n in range(args.reps)]
    print(f"reviews: {len(jobs)}", flush=True)
    if args.dry_run:
        return 0
    out = ROOT / "eval/results/lab" / args.tag
    out.mkdir(parents=True, exist_ok=True)
    sail, rows = SailClient(), []
    with ThreadPoolExecutor(max_workers=args.parallel) as pool:
        futs = [pool.submit(review, sail, *j) for j in jobs]
        for i, fut in enumerate(as_completed(futs), 1):
            rows.append(fut.result())
            if i % 50 == 0:
                print(f"[{i}/{len(jobs)}]", flush=True)
    (out / "rows.json").write_text(json.dumps(rows, indent=1))
    return report(out)


if __name__ == "__main__":
    raise SystemExit(main())
