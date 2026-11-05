"""Lay out SlopCodeBench's Harbor tasks the way the EvoCode runner reads them.

Input is the output of scb-problems' scripts/scb_to_harbor.py. Tests and the image
are copied as they are. Each spec is wrapped in the words of the published runner's
configs/prompts/just-solve.jinja, because the graded server starts from the agent's
virtual environment and the Harbor specs do not say to make one.
"""

from __future__ import annotations

import argparse
import shutil
import sys
import tomllib
from pathlib import Path


FIRST = ("Use a virtual environment and ensure that a `requirements.txt` is present with "
         "any dependencies you need to solve the problem.")
LATER = ("Keep using the same virtual environment you started with, update "
         "`requirements.txt` with any new dependencies you need.")


def wrap(spec: str, first: bool) -> str:
    return ("Implement a program that 100% solves the specification. That is all you need "
            f"to do.\n{FIRST if first else LATER}\n\nYour task is:\n{spec.strip()}\n")


def prepare(src: Path, out: Path) -> Path:
    cfg = tomllib.loads((src / "task.toml").read_text())
    name = cfg["metadata"]["source_problem"]
    steps = [s["name"] for s in cfg["steps"]]
    dest = out / name
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(src / "environment", dest / "environment")
    for n, step in enumerate(steps, start=1):
        target = dest / "steps" / f"round-{n}"
        target.mkdir(parents=True)
        spec = (src / "steps" / step / "instruction.md").read_text()
        (target / "instruction.md").write_text(wrap(spec, n == 1))
        shutil.copytree(src / "steps" / step / "solution", target / "solution")
        # Harbor lays the step's test.sh over the shared tests.
        shutil.copytree(src / "tests", target / "tests")
        shutil.copytree(src / "steps" / step / "tests", target / "tests", dirs_exist_ok=True)
    chain = "\n".join(f'[[metadata.requirement_chain.steps]]\nstep = "round-{n}"\nchange_types = []'
                      for n in range(1, len(steps) + 1))
    # Each step sets its own limits, and the largest one holds for every round.
    agent = max(s["agent"]["timeout_sec"] for s in cfg["steps"])
    verifier = max(s["verifier"]["timeout_sec"] for s in cfg["steps"])
    (dest / "task.toml").write_text(
        f'[metadata]\nname = "{name}"\nbenchmark = "slopcodebench"\n\n'
        f"[agent]\ntimeout_sec = {agent}\n\n[verifier]\ntimeout_sec = {verifier}\n\n{chain}\n")
    return dest


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--harbor", required=True, type=Path, help="folder of converted tasks")
    ap.add_argument("--out", required=True, type=Path)
    args = ap.parse_args()
    tasks = sorted(p.parent for p in args.harbor.glob("*/task.toml"))
    if not tasks:
        print(f"no tasks under {args.harbor}", file=sys.stderr)
        return 1
    for task in tasks:
        print(prepare(task, args.out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
