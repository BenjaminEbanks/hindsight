"""Analyze swarm-eval results: per-task x arm outcomes, comparison against
baseline, controls, health and provenance.

Usage:
  uv run python scripts/analyze_swarm.py eval/results/swarm/<dir> [<dir> ...]
  uv run python scripts/analyze_swarm.py eval/results/swarm/calib1 --calibrate

The primary outcome is per EPISODE: did every hidden test pass. Test counts
are shown as passed/expected only for colour.

Honesty rules: episodes that did not run are reported and excluded from rates;
nothing is imputed. Reps are independent runs, so arms are compared as groups:
rep r0 of one arm has no relationship to rep r0 of another.
"""
from __future__ import annotations

import argparse
import ast
import glob
import json
import os
import re
from collections import defaultdict
from math import comb, sqrt

BASELINE = "baseline_alone"
CEILING = "full_context"
FAILED_LINE = re.compile(r"^(?:FAILED|ERROR) (\S+?)(?:\s*-\s*(.*))?$", re.M)

# Calibration: a task can only show a Hindsight effect if late requirements
# hurt (streamed scores low) and the model can do it with foresight (full
# context scores high).
MAX_STREAMED_PASS = 0.6
MIN_CEILING_PASS = 0.8
MIN_CALIBRATION_N = 8


def load(dirs: list[str]) -> list[dict]:
    eps = []
    for out_dir in dirs:
        for p in sorted(glob.glob(os.path.join(out_dir, "*__*.json"))):
            ep = json.load(open(p))
            name = os.path.basename(p)[:-5]
            parts = name.rsplit("__", 2)
            if len(parts) != 3:
                continue
            ep["_task"], ep["_arm"], ep["_rep"] = parts
            ep["_file"] = f"{os.path.basename(out_dir.rstrip('/'))}/{name}"
            eps.append(ep)
    return eps


def final_of(ep: dict) -> dict:
    """A fork payload keeps its result under `treatment`."""
    return ep.get("final") or (ep.get("treatment") or {}).get("final") or {}


def nudges_of(ep: dict) -> list:
    return ep.get("nudges") or (ep.get("treatment") or {}).get("nudges") or []


def ran(ep: dict) -> bool:
    return bool(final_of(ep).get("ran")) and not ep.get("quiet_error")


def passed_all(ep: dict) -> bool:
    f = final_of(ep)
    if "ok" in f:
        return bool(f["ok"])
    return (f.get("failed") or 0) == 0 and (f.get("passed") or 0) > 0


def expected_tests(task: str, tasks_dir: str) -> int | None:
    path = os.path.join(tasks_dir, f"{task}.json")
    if not os.path.exists(path):
        return None
    n = 0
    for stage in json.load(open(path))["stages"]:
        for code in (stage.get("hidden_tests") or {}).values():
            n += sum(1 for node in ast.walk(ast.parse(code))
                     if isinstance(node, ast.FunctionDef)
                     and node.name.startswith("test_"))
    return n


def is_control(task: str, tasks_dir: str) -> bool:
    path = os.path.join(tasks_dir, f"{task}.json")
    return os.path.exists(path) and bool(json.load(open(path)).get("no_change"))


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if not n:
        return 0.0, 1.0
    p = k / n
    den = 1 + z * z / n
    mid = (p + z * z / (2 * n)) / den
    half = z * sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    return max(0.0, mid - half), min(1.0, mid + half)


def fisher(a: int, b: int, c: int, d: int) -> float:
    """Two-sided exact test on [[a, b], [c, d]]."""
    n, row, col = a + b + c + d, a + b, a + c
    prob = lambda x: comb(col, x) * comb(n - col, row - x) / comb(n, row)  # noqa: E731
    observed = prob(a)
    return sum(prob(x) for x in range(max(0, row - (n - col)), min(row, col) + 1)
               if prob(x) <= observed + 1e-12)


def rate(eps: list[dict]) -> tuple[int, int]:
    good = [e for e in eps if ran(e)]
    return sum(passed_all(e) for e in good), len(good)


def provenance_section(eps: list[dict]) -> None:
    print("provenance:")
    groups = defaultdict(list)
    for e in eps:
        p = e.get("provenance") or {}
        # Comparability comes from what shipped, not the commit it came from.
        # gate_mode only appears in results from before there was one mode.
        key = (p.get("content_sha256", "")[:12] or "NONE",
               p.get("model", ""), p.get("worker_model", ""),
               p.get("gate_mode", ""), p.get("stage_gap", ""))
        groups[key].append(e)
    for (content, model, worker, mode, gap), members in sorted(groups.items()):
        print(f"  content={content} model={model or '-'} worker={worker or '-'} "
              f"gate={mode or '-'} gap={gap or '-'}: {len(members)} episodes")
    if len(groups) > 1 or ("NONE", "", "", "", "") in groups:
        print("  WARNING: not a single known vintage; these rows may not be "
              "comparable.")


def calibrate(by: dict, tasks: list[str]) -> None:
    print(f"\ncalibration (keep: streamed <= {MAX_STREAMED_PASS:.0%}, "
          f"full context >= {MIN_CEILING_PASS:.0%}, n >= {MIN_CALIBRATION_N}):")
    kept = []
    for t in tasks:
        sk, sn = rate(by.get((t, BASELINE), []))
        fk, fn = rate(by.get((t, CEILING), []))
        if min(sn, fn) < MIN_CALIBRATION_N:
            verdict = "NOT ENOUGH RUNS"
        elif sk / sn <= MAX_STREAMED_PASS and fk / fn >= MIN_CEILING_PASS:
            verdict = "KEEP"
            kept.append(t)
        elif fk / fn < MIN_CEILING_PASS:
            verdict = "drop: fails even with full context (review the task)"
        else:
            verdict = "drop: streaming already passes, no room to improve"
        print(f"  {t:<12} streamed {sk}/{sn}  full context {fk}/{fn}  {verdict}")
    print(f"tasks that can show an effect: {kept or 'NONE'}")
    if not kept:
        print("With none, do not run worker fleets: author tasks with real "
              "lock-in first.")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("dirs", nargs="*", default=["eval/results/swarm/new"])
    ap.add_argument("--tasks-dir", default="eval/tasks/v2")
    ap.add_argument("--calibrate", action="store_true",
                    help="print the keep/drop verdict per task")
    args = ap.parse_args()

    eps = load(args.dirs)
    by = defaultdict(list)
    for ep in eps:
        by[(ep["_task"], ep["_arm"])].append(ep)
    tasks = sorted({t for t, _ in by})
    arms = sorted({a for _, a in by})
    print(f"episodes: {len(eps)} over {len(tasks)} tasks x {len(arms)} arms\n")
    provenance_section(eps)

    print("\nall hidden tests passed, by task x arm (episodes):")
    print(f"{'task':<12}" + "".join(f"{a:>20}" for a in arms))
    for t in tasks:
        row = f"{t:<12}"
        for a in arms:
            k, n = rate(by.get((t, a), []))
            row += f"{k}/{n}".rjust(20) if by.get((t, a)) else "-".rjust(20)
        print(row)

    print("\nby arm, pooled (a guide only: tasks differ, see per-task rows):")
    for a in arms:
        pool = [e for t in tasks for e in by.get((t, a), [])]
        k, n = rate(pool)
        lo, hi = wilson(k, n)
        print(f"  {a:<20} {k}/{n}  95% CI {lo:.0%}-{hi:.0%}")

    if BASELINE in arms:
        print(f"\ncompared with {BASELINE} (same task; exact test on episodes):")
        for a in [x for x in arms if x != BASELINE]:
            for t in tasks:
                if not by.get((t, a)) or not by.get((t, BASELINE)):
                    continue
                k1, n1 = rate(by[(t, a)])
                k0, n0 = rate(by[(t, BASELINE)])
                if n0 and n1:
                    p = fisher(k1, n1 - k1, k0, n0 - k0)
                    print(f"  {t:<12} {a:<20} {k1}/{n1} vs {k0}/{n0}  p={p:.3f}")

    print("\nhidden tests passed / expected, and episodes that never built "
          "the code:")
    for t in tasks:
        expected = expected_tests(t, args.tasks_dir)
        for a in arms:
            good = [e for e in by.get((t, a), []) if ran(e)]
            if not good:
                continue
            passed = [final_of(e).get("passed") or 0 for e in good]
            broken = sum(
                1 for e in good
                if final_of(e).get("collection_errors")
                or "ModuleNotFoundError" in (final_of(e).get("output_tail") or "")
                or "ImportError" in (final_of(e).get("output_tail") or ""))
            print(f"  {t:<12} {a:<20} passed {passed} of {expected or '?'}"
                  f"  import/collection failures: {broken}")

    print("\nfailing tests by task x arm:")
    for t in tasks:
        for a in arms:
            fails = defaultdict(int)
            for e in by.get((t, a), []):
                f = final_of(e)
                if f.get("tests"):
                    for test in f["tests"]:
                        if test["outcome"] != "passed":
                            fails[test["name"].split(".")[-1]] += 1
                else:
                    for m in FAILED_LINE.finditer(f.get("output_tail") or ""):
                        fails[m.group(1).split("::")[-1]] += 1
            if fails:
                top = ", ".join(f"{k} x{v}" for k, v in
                                sorted(fails.items(), key=lambda x: -x[1]))
                print(f"  {t:<12} {a:<20} {top}")

    print("\nepisodes that did not run (excluded from rates):")
    missing = [e for e in eps if not ran(e)]
    for e in missing:
        print(f"  {e['_file']}: {(final_of(e).get('error') or e.get('quiet_error') or '')[:100]}")
    if not missing:
        print("  none")

    pipeline = [a for a in arms if a.startswith("treatment") or a == "fork"]
    if pipeline:
        print("\nnudges on no-change controls (the right number is 0):")
        controls = [t for t in tasks if is_control(t, args.tasks_dir)]
        for t in controls:
            for a in pipeline:
                es = [e for e in by.get((t, a), []) if ran(e)]
                if es:
                    print(f"  {t:<12} {a:<20} "
                          f"{sum(len(nudges_of(e)) for e in es)} nudges over "
                          f"{len(es)} episodes")
        if not controls:
            print("  no control tasks in this set")

        print("\nworker health and pipeline spend:")
        for t in tasks:
            for a in pipeline:
                es = by.get((t, a), [])
                if not es:
                    continue
                health = [e.get("worker_health") or {} for e in es]
                total = sum(h.get("explorations_total") or 0 for h in health)
                good = sum(h.get("explorations_succeeded") or 0 for h in health)
                bad = sum(h.get("explorations_errored") or 0 for h in health)
                flagged = [e["_file"] for e, h in zip(es, health)
                           if h.get("workers_broken") or h.get("workers_degraded")]
                spend = sum(e.get("spend_usd") or 0 for e in es)
                print(f"  {t:<12} {a:<20} workers ok {good}/{total}, errored "
                      f"{bad} | ${spend:.2f} | flagged: {flagged or 'none'}")

    tokens = defaultdict(lambda: [0, 0])
    for e in eps:
        used = e.get("main_agent_tokens")
        if used:
            tokens[e["_arm"]][0] += sum(used.values())
            tokens[e["_arm"]][1] += 1
    print("\nmain-agent tokens per episode (not in spend_usd):")
    for a, (total, n) in sorted(tokens.items()):
        print(f"  {a:<20} {total // n} over {n} episodes")
    if not tokens:
        print("  not recorded (results predate token recording; spend_usd "
              "excludes the main agent, so $0.00 does not mean free)")

    if args.calibrate:
        calibrate(by, tasks)


if __name__ == "__main__":
    main()
