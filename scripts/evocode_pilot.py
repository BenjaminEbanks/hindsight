#!/usr/bin/env python3
"""Run EvoCode-Bench tasks in Sail boxes.

    --oracle     apply each round's reference solution and run its tests. No model
                 spend. Proves the box matches the benchmark's environment: if the
                 reference fails here, agent scores from this setup mean nothing.
    (default)    OpenCode works through the rounds in one session.
    --hindsight  the same, with Hindsight watching from this machine, which must
                 stay awake. Designs reach the agent at gaps between rounds. Every
                 model call uses --model. Its timeline is saved as hs/<task>/trace.txt.

One box per task. Each round follows the benchmark: the tests are absent while
the agent works, copied in only to grade, and removed again. A failed round does
not stop the session; the fail-stop score is computed from the same rounds.

The dataset is CC BY-NC and is not kept in this repo: pass its folder with --data.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import tomllib
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import sail  # noqa: E402

from hindsight.eval.episode import SIMULATED_CONSENT, EvalEpisode  # noqa: E402
from hindsight.eval.harness import _extract_reply, _scan_events  # noqa: E402
from hindsight.eval.tasks import Stage, Task  # noqa: E402
from hindsight.sail_client import SailClient  # noqa: E402

# By full path: some images carry a different opencode earlier on the PATH.
OPENCODE = "/opt/oc/opencode"
OC_URL = ("https://github.com/anomalyco/opencode/releases/download/"
          "v2.2.5/opencode-linux-x64.tar.gz")
# Only tools are added to the benchmark's image; the oracle run covers this.
AGENT_TOOLS = f"""
RUN apt-get update && apt-get install -y --no-install-recommends curl tar gzip git ca-certificates && rm -rf /var/lib/apt/lists/*
RUN mkdir -p /opt/oc /root/.config/opencode && curl -fsSL {OC_URL} | tar -xz -C /opt/oc && ln -sf /opt/oc/opencode /usr/local/bin/opencode
"""
# Placeholder prices from hindsight/budget.py: an estimate, not a bill.
USD_PER_M_INPUT, USD_PER_M_OUTPUT = 0.6, 2.2
FLASH_USD_PER_M = (0.1, 0.4)
# Sail's listed balanced prices on 2026-11-03: input, output, cached input.
GEMMA_USD_PER_M = (0.11, 0.32, 0.06)
# Saved once with Sail: adds the key from Sail's secret store to model calls and
# nothing else, so no box holds it. An agent read its environment's key on a past run.
BOX_POLICY = "hindsight-inference-only"
BOX_KEY_PLACEHOLDER = "not-a-real-key"
NOT_CODE = (".venv", "venv", "__pycache__", ".pytest_cache", ".ruff_cache", ".mypy_cache")
TAR_EXCLUDES = " ".join(f"--exclude={name}" for name in (".git", *NOT_CODE))
HINDSIGHT_ROLES = ("worker", "judge", "reviewer", "nudge")
# Longer than a design takes to build, shorter than an idle hang: the last round's
# design still gets to land before scoring.
HINDSIGHT_FINAL_WAIT = 240 * 60
# Under the kernel's limit for one command-line argument (128 KB).
MAX_DIFF_CHARS = 90_000
BOX_LIFETIME = 12 * 3600


class Spend:
    """Stops the whole pilot at a limit. Without a listed cached rate, cached
    input counts at the full input price, which errs high."""

    def __init__(self, limit: float):
        self.limit, self.usd, self.lock = limit, 0.0, threading.Lock()

    def add(self, tokens: dict, rates=(USD_PER_M_INPUT, USD_PER_M_OUTPUT)) -> None:
        with self.lock:
            self.usd += _usd(tokens, rates)

    def over(self) -> bool:
        with self.lock:
            return self.usd >= self.limit


def usd_rates(model: str) -> tuple[float, float]:
    if "gemma-4-31b" in model.lower():
        return GEMMA_USD_PER_M
    return FLASH_USD_PER_M if "flash" in model.lower() else (USD_PER_M_INPUT, USD_PER_M_OUTPUT)


def _usd(tokens: dict, rates=(USD_PER_M_INPUT, USD_PER_M_OUTPUT)) -> float:
    cached = rates[2] if len(rates) > 2 else rates[0]
    return ((tokens["input"] + tokens["cache_write"]) * rates[0]
            + tokens["cache_read"] * cached
            + (tokens["output"] + tokens["reasoning"]) * rates[1]) / 1e6


def _cache_tokens(raw: str) -> dict:
    """OpenCode reports cached input apart from `input`, so input alone undercounts."""
    read = sum(int(v) for v in re.findall(r'"cache":\s*\{[^}]*"read":\s*(\d+)', raw))
    write = sum(int(v) for v in re.findall(r'"cache":\s*\{[^}]*"write":\s*(\d+)', raw))
    return {"cache_read": read, "cache_write": write}
POLL_SECONDS = 15


def opencode_config(model: str | None = None) -> str:
    """The local provider settings, with the key read from the environment so
    no key is written to a box the agent can look around in."""
    cfg = json.loads((Path.home() / ".config/opencode/opencode.json").read_text())
    for provider in cfg.get("provider", {}).values():
        options = provider.get("options") or {}
        if "apiKey" in options:
            options["apiKey"] = "{env:SAIL_API_KEY}"
    if model and "/" in model:
        provider, name = model.split("/", 1)
        cfg["provider"][provider].setdefault("models", {}).setdefault(name, {"name": name})
    return json.dumps(cfg)


def round_numbers(task_dir: Path) -> list[int]:
    return sorted(int(p.name.split("-")[1]) for p in (task_dir / "steps").glob("round-*"))


def task_config(task_dir: Path) -> dict:
    return tomllib.loads((task_dir / "task.toml").read_text())


def build_image(task_dir: Path):
    env = task_dir / "environment"
    return sail.Image.from_dockerfile(
        contents=(env / "Dockerfile").read_text() + AGENT_TOOLS, context_dir=str(env),
        architecture="amd64")


def upload_dir(client: SailClient, box_id: str, local: Path, dest: str) -> None:
    with tempfile.NamedTemporaryFile(suffix=".tar.gz") as f:
        with tarfile.open(f.name, "w:gz") as tar:
            tar.add(local, arcname=".")
        client.upload_file(box_id, f.name, "/work/up.tar.gz")
        sh(client, box_id, f"mkdir -p {dest} && tar -xzf /work/up.tar.gz -C {dest} "
                           "&& rm /work/up.tar.gz")


def sh(client: SailClient, box_id: str, cmd: str, timeout: int = 300):
    # Not a login shell: that would reset the PATH the Dockerfile set.
    return client.run(box_id, ["bash", "-c", cmd], timeout=timeout)


def run_long(client: SailClient, box_id: str, cmd: str, limit: float, env=None) -> int:
    """Long commands run detached, since a long synchronous exec can drop."""
    tag = uuid.uuid4().hex[:8]
    client.start_background(
        box_id, f"bash -c {shlex.quote(cmd)}; echo $? > /work/{tag}.exit", env=env)
    deadline = time.time() + limit + 120
    while time.time() < deadline:
        time.sleep(POLL_SECONDS)
        r = sh(client, box_id, f"cat /work/{tag}.exit 2>/dev/null || true", timeout=60)
        if (r.stdout or "").strip():
            return int(r.stdout.strip())
    raise TimeoutError(f"command still running after {limit}s")


# Must agree with selection._is_test_path. The published score counts tests, so adding
# tests alone improves it: a worker's test file took verbosity from .28 to .14 with no
# change to the product code.
PRODUCT_EXCLUDES = " ".join(
    f"--exclude='{name}'" for name in (
        ".git", *NOT_CODE, "tests", "test", "__tests__", "testdata", "test_*", "test-*",
        "test.*", "*_test.*", "*.test.*", "*_spec.*", "*.bats", "conftest.py"))
SCB_CHECK_PATH = "/opt/uv-tools/scb-check/bin"


def product_quality(client: SailClient, box_id: str) -> dict:
    """The benchmark's own scorer on /app with the tests left out. A failure is
    recorded as no score, never as a number."""
    r = sh(client, box_id,
           "rm -rf /work/product && mkdir -p /work/product "
           f"&& tar {PRODUCT_EXCLUDES} -cf - -C /app . | tar -xf - -C /work/product "
           f"&& PATH={SCB_CHECK_PATH}:$PATH scb-check check /work/product --report "
           "--include-all 2>/dev/null; code=$?; rm -rf /work/product; exit $code",
           timeout=300)
    try:
        d = json.loads(r.stdout or "")
        return {"verbosity_product": d["verbosity"], "erosion_product": d["erosion"],
                "loc_product": d.get("total_loc")}
    except (ValueError, KeyError, TypeError):
        return {"verbosity_product": None, "erosion_product": None, "loc_product": None}


def save_checkpoints(client: SailClient, box_id: str, out: Path, name: str) -> None:
    """Every checkpoint's code, so it can be rescored after the box is gone."""
    path = out / "checkpoints" / f"{name}.tar.gz"
    path.parent.mkdir(exist_ok=True)
    sh(client, box_id, "tar -czf /work/shadow.tar.gz -C /work shadow.git", timeout=300)
    client.download_file(box_id, "/work/shadow.tar.gz", str(path))
    sh(client, box_id, "rm -f /work/shadow.tar.gz")


def grade(client: SailClient, box_id: str, task_dir: Path, n: int, limit: float,
          log_path: Path | None = None) -> dict:
    upload_dir(client, box_id, task_dir / "steps" / f"round-{n}" / "tests", "/tests")
    t0 = time.time()
    code = run_long(client, box_id, "mkdir -p /logs/verifier; cd /app && bash /tests/test.sh "
                    "> /work/verify.log 2>&1", limit)
    reward = sh(client, box_id, "cat /logs/verifier/reward.txt 2>/dev/null || true").stdout
    details = sh(client, box_id,
                 "cat /logs/verifier/reward_details.json 2>/dev/null || true").stdout or ""
    log = sh(client, box_id, "cat /work/verify.log", timeout=120).stdout or ""
    # All of it, including the log: an agent found and read one left in /work.
    sh(client, box_id, "rm -rf /tests /logs/verifier /work/verify.log")
    if log_path is not None:
        log_path.write_text(log)
    m = re.search(r"CASE_SUMMARY total_cases=(\d+) success_count=(\d+) fail_count=(\d+)", log)
    failed = [line.strip()[:600] for line in log.splitlines()
              if "CASE_RESULT" in line and "status=fail" in line][:30]
    if details.strip():
        graded = slopcode_grade(json.loads(details))
        graded["slopcode"].update(product_quality(client, box_id))
        return {**graded, "exit": code, "verify_seconds": round(time.time() - t0)}
    return {"passed": (reward or "").strip() == "1.0", "exit": code,
            "cases_total": int(m.group(1)) if m else None,
            "cases_passed": int(m.group(2)) if m else None,
            "failed_cases": failed, "verify_seconds": round(time.time() - t0)}


def slopcode_grade(d: dict) -> dict:
    """SlopCodeBench reports pass rates and quality scores, not a case summary.
    A round passes when every test passes, including earlier rounds' tests."""
    total = sum(int(d.get(f"{g}_collected") or 0)
                for g in ("core", "functionality", "error", "regression"))
    passed = sum(int(d.get(f"{g}_passed") or 0)
                 for g in ("core", "functionality", "error", "regression"))
    keys = ("core_pass_rate", "isolated_pass_rate", "strict_pass_rate", "regression_pass_rate",
            "verbosity", "erosion", "infrastructure_failure")
    return {"passed": float(d.get("strict_pass_rate") or 0) >= 0.999,
            "cases_total": total, "cases_passed": passed, "failed_cases": [],
            "slopcode": {k: d.get(k) for k in keys}}


def send_turn(client: SailClient, box_id: str, text: str, label: str, session: str,
              model: str, limit: float, raw_dir: Path) -> dict:
    client.write_files(box_id, {"/work/prompt.md": text})
    cmd = (f"cd /app && export PWD=/app && timeout {int(limit)} {OPENCODE} run "
           f"--standalone --session {session} --auto --format json --model {model} "
           '"$(cat /work/prompt.md)" > /work/out.jsonl 2> /work/err.txt')
    t0 = time.time()
    totals = {"input": 0, "output": 0, "reasoning": 0, "cache_read": 0, "cache_write": 0}
    errors: list[str] = []
    timed_out = False
    for attempt in range(3):  # provider failures are retried and recorded
        code = run_long(client, box_id, cmd, limit,
                        env={"SAIL_API_KEY": BOX_KEY_PLACEHOLDER})
        raw = sh(client, box_id, "cat /work/out.jsonl", timeout=180).stdout or ""
        (raw_dir / f"{label}-attempt-{attempt}.jsonl").write_text(raw)
        found, tokens = _scan_events(raw)
        tokens.update(_cache_tokens(raw))
        for k in totals:
            totals[k] += tokens[k]
        errors += found
        timed_out = code == 124
        if timed_out or (code == 0 and not found):
            break
        if code != 0:
            err = sh(client, box_id, "tail -c 600 /work/err.txt").stdout
            errors.append(f"exit {code}: {err}")
        time.sleep(30 * (attempt + 1))
    sh(client, box_id, "rm -f /work/prompt.md")
    sessions = sorted(set(re.findall(r'"sessionID":"(ses[^"]+)"', raw)))
    if not (totals["input"] + totals["cache_read"]):
        # Grading untouched code would look like a real low score.
        raise RuntimeError(f"{label}: the agent never ran: {errors[-1:]}")
    return {"agent_seconds": round(time.time() - t0), "tokens": totals, "errors": errors,
            "timed_out": timed_out, "attempts": attempt + 1, "sessions": sessions,
            "reply": _extract_reply(raw)[-3000:]}


def record_code(client: SailClient, box_id: str, n: int) -> dict:
    """A commit outside /app, so the agent's repo is untouched."""
    git = "git --git-dir=/work/shadow.git --work-tree=/app"
    r = sh(client, box_id, f"{git} add -A && {git} commit -q --allow-empty -m round-{n} "
                           f"&& {git} show --shortstat --format= HEAD", timeout=120)
    numbers = re.findall(r"(\d+) (files? changed|insertions?|deletions?)", r.stdout or "")
    return {kind.split()[0].rstrip("s"): int(v) for v, kind in numbers}


def prepare_box(client: SailClient, box_id: str) -> None:
    sh(client, box_id, "mkdir -p /work && git config --global user.email pilot@local "
                       "&& git config --global user.name pilot "
                       # SlopCodeBench's grader hands /app to another user.
                       "&& git config --global --add safe.directory '*' "
                       "&& cd /app && git init -q && git add -A "
                       "&& git commit -q --allow-empty -m base "
                       "&& git init -q --bare /work/shadow.git "
                       f"&& printf '%s\\n' {' '.join(NOT_CODE)} "
                       ">> /work/shadow.git/info/exclude")


def write_opencode_config(client: SailClient, box_id: str, model: str) -> None:
    """In the box's own home, which some benchmark images set away from /root."""
    home = (sh(client, box_id, 'mkdir -p "$HOME/.config/opencode" && echo "$HOME"').stdout
            or "").strip()
    if not home.startswith("/"):
        raise RuntimeError(f"box has no usable home: {home[:80]!r}")
    client.write_files(box_id, {f"{home}/.config/opencode/opencode.json":
                                opencode_config(model)})


def check_opencode(client: SailClient, box_id: str) -> None:
    version = sh(client, box_id, f"{OPENCODE} --version").stdout or ""
    if "2.2.5" not in version:
        raise RuntimeError(f"wrong OpenCode in the box: {version[:80]}")


def run_task(client: SailClient, task_dir: Path, *, oracle: bool, model: str, out: Path,
             max_rounds: int | None, spend: Spend) -> dict:
    cfg = task_config(task_dir)
    name = cfg["metadata"]["name"]
    chain = {s["step"]: s.get("change_types", [])
             for s in cfg["metadata"]["requirement_chain"]["steps"]}
    numbers = round_numbers(task_dir)[:max_rounds]
    raw_dir = out / "raw" / name
    raw_dir.mkdir(parents=True, exist_ok=True)
    result = {"task": name, "mode": "oracle" if oracle else "agent", "model": model,
              "rounds": [], "error": ""}
    box = None
    t0 = time.time()
    try:
        box = client.create_box(f"evocode-{name}"[:50], image=build_image(task_dir),
                                max_lifetime_seconds=BOX_LIFETIME, size=client.main_box_size)
        result["build_seconds"] = round(time.time() - t0)
        prepare_box(client, box.id)
        if not oracle:
            write_opencode_config(client, box.id, model)
            check_opencode(client, box.id)
        session = f"ses-evocode-{uuid.uuid4().hex[:10]}"
        for n in numbers:
            if spend.over():
                result["error"] = f"stopped: spend limit ${spend.limit} reached"
                break
            step = f"round-{n}"
            row = {"round": n, "change_types": chain.get(step, [])}
            if oracle:
                upload_dir(client, box.id, task_dir / "steps" / step / "solution", "/solution")
                row["solve_exit"] = run_long(
                    client, box.id, "cd /app && bash /solution/solve.sh > /work/solve.log 2>&1",
                    float(cfg["agent"]["timeout_sec"]))
                sh(client, box.id, "rm -rf /solution")
            else:
                text = (task_dir / "steps" / step / "instruction.md").read_text()
                row.update(send_turn(client, box.id, text, step, session, model,
                                     float(cfg["agent"]["timeout_sec"]), raw_dir))
                spend.add(row["tokens"], usd_rates(model))
            row["code"] = record_code(client, box.id, n)
            row.update(grade(client, box.id, task_dir, n,
                             float(cfg["verifier"]["timeout_sec"])))
            result["rounds"].append(row)
            write_result(out, result, numbers)
            print(f"{name} round {n}: {'pass' if row['passed'] else 'FAIL'} "
                  f"{row.get('cases_passed')}/{row.get('cases_total')}", flush=True)
        final = out / "final" / f"{name}.tar.gz"
        final.parent.mkdir(exist_ok=True)
        sh(client, box.id, "tar --exclude=.git -czf /work/final.tar.gz -C /app .")
        client.download_file(box.id, "/work/final.tar.gz", str(final))
    except Exception as e:  # noqa: BLE001 - recorded, never faked
        result["error"] = f"{type(e).__name__}: {e}"[:1500]
    finally:
        result["wall_seconds"] = round(time.time() - t0)
        if box is not None:
            try:
                save_checkpoints(client, box.id, out, name)
            except Exception as e:  # noqa: BLE001 - recorded, never faked
                result["error"] += f" | checkpoints not saved: {type(e).__name__}: {e}"[:300]
            try:
                client.terminate(box.id)
            except Exception as e:  # noqa: BLE001
                result["error"] += f" | terminate failed for {box.id}: {e}"
    write_result(out, result, numbers)
    return result


class BoxAgent:
    """The main agent for an episode: OpenCode in the task's box. The episode sends
    it user messages, and its code is mirrored here for Hindsight to snapshot."""

    def __init__(self, client: SailClient, box_id: str, task_dir: Path, cfg: dict,
                 numbers: list[int], session: str, model: str, raw_dir: Path, repo: Path,
                 result: dict, out: Path, spend: Spend):
        self.client, self.box_id, self.task_dir, self.cfg = client, box_id, task_dir, cfg
        self.numbers, self.session, self.model = numbers, session, model
        self.raw_dir, self.repo, self.result, self.out, self.spend = (
            raw_dir, repo, result, out, spend)
        self.rounds = {(task_dir / "steps" / f"round-{n}" / "instruction.md").read_text(): n
                       for n in numbers}
        self.chain = {s["step"]: s.get("change_types", [])
                      for s in cfg["metadata"]["requirement_chain"]["steps"]}
        self.last_round: int | None = None
        self.extra_turns = 0
        self.token_totals = {"input": 0, "output": 0, "reasoning": 0}
        self.errors: list[str] = []

    def send(self, message: str) -> str:
        if self.spend.over():
            raise RuntimeError(f"stopped: spend limit ${self.spend.limit} reached")
        n = self.rounds.get(message)
        if n is None:
            self.extra_turns += 1
        label = f"round-{n}" if n else f"hindsight-{self.extra_turns}"
        limit = float(self.cfg["agent"]["timeout_sec"])
        row = send_turn(self.client, self.box_id, message, label, self.session, self.model,
                        limit, self.raw_dir)
        self.spend.add(row["tokens"], usd_rates(self.model))
        for k in self.token_totals:
            self.token_totals[k] += row["tokens"][k]
        self.errors += row["errors"]
        mirror_code(self.client, self.box_id, self.repo)
        verifier_limit = float(self.cfg["verifier"]["timeout_sec"])
        if n:
            self.last_round = n
            row.update(round=n, change_types=self.chain.get(f"round-{n}", []),
                       code=record_code(self.client, self.box_id, n))
            row.update(grade(self.client, self.box_id, self.task_dir, n, verifier_limit,
                             self.raw_dir / f"round-{n}-verify.log"))
            self.result["rounds"].append(row)
            print(f"{self.result['task']} round {n}: {'pass' if row['passed'] else 'FAIL'} "
                  f"{row.get('cases_passed')}/{row.get('cases_total')}", flush=True)
        else:
            consent = message == SIMULATED_CONSENT
            row.update(kind="consent" if consent else "delivery",
                       after_round=self.last_round, message_head=message[:600])
            if consent and self.last_round:
                # Did applying it break what this round had reached?
                row["code"] = record_code(self.client, self.box_id, self.last_round)
                row["grade_after"] = grade(self.client, self.box_id, self.task_dir,
                                           self.last_round, verifier_limit)
            self.result["hindsight_turns"].append(row)
            print(f"{self.result['task']} {row['kind']} after round {self.last_round}",
                  flush=True)
        write_result(self.out, self.result, self.numbers)
        return row["reply"]

    def share_file(self, name: str, text: str) -> str:
        """Somewhere the agent can read what didn't fit in a message."""
        path = f"/work/hindsight/{name}"
        sh(self.client, self.box_id, "mkdir -p /work/hindsight")
        self.client.write_files(self.box_id, {path: text})
        return path

    def close(self) -> None:
        pass


def _keep_safe(member: tarfile.TarInfo, dest: str):
    """Skips one entry that can't be safely unpacked, such as a link out of the tree.
    The agent's code is still mirrored, and one odd link shouldn't end the task."""
    try:
        return tarfile.data_filter(member, dest)
    except tarfile.FilterError:
        return None


def mirror_code(client: SailClient, box_id: str, repo: Path) -> None:
    # Installed packages and caches aren't the agent's code, and Hindsight reads this.
    sh(client, box_id, f"tar {TAR_EXCLUDES} -czf /work/mirror.tar.gz -C /app .", timeout=300)
    with tempfile.NamedTemporaryFile(suffix=".tar.gz") as f:
        client.download_file(box_id, "/work/mirror.tar.gz", f.name)
        sh(client, box_id, "rm -f /work/mirror.tar.gz")
        for path in repo.iterdir():
            if path.name != ".git":
                shutil.rmtree(path) if path.is_dir() and not path.is_symlink() else path.unlink()
        with tarfile.open(f.name) as tar:
            tar.extractall(repo, filter=_keep_safe)


FORCED_REASON = ("Look for what would be built differently knowing everything the user "
                 "has asked for.")


def forced_review(at_round: int, result: dict):
    """Says yes once, at a round chosen ahead of time. The task's result is marked,
    and write_result keeps the mark."""
    calls = {"n": 0}

    def fn(prompt: str) -> str:
        calls["n"] += 1  # the first review is on round 2
        if calls["n"] + 1 >= at_round and "forced_review_turn" not in result:
            result["forced_review_turn"] = calls["n"] + 1
            return json.dumps({"latest_request_needs": "forced", "what_was_true_before": "forced",
                               "what_no_longer_holds": FORCED_REASON,
                               "call_background": True})
        return json.dumps({"latest_request_needs": "skipped", "what_was_true_before": "nothing",
                           "what_no_longer_holds": "nothing",
                           "call_background": False})

    return fn


def middle_round(task_dir: Path, max_rounds: int | None = None) -> int:
    """Round 3 of 5, round 2 of 4."""
    return (len(round_numbers(task_dir)[:max_rounds]) + 1) // 2


def run_task_with_hindsight(client: SailClient, task_dir: Path, *, model: str, out: Path,
                            max_rounds: int | None, spend: Spend, gap: float,
                            force_review_at: int | None = None,
                            wait_for_run: bool = True) -> dict:
    cfg = task_config(task_dir)
    name = cfg["metadata"]["name"]
    numbers = round_numbers(task_dir)[:max_rounds]
    raw_dir = out / "raw" / name
    raw_dir.mkdir(parents=True, exist_ok=True)
    repo = out / "repos" / name
    repo.mkdir(parents=True, exist_ok=True)
    result = {"task": name, "mode": "hindsight", "model": model, "rounds": [],
              "hindsight_turns": [], "error": ""}
    bare = model.split("/", 1)[1] if model.startswith("sail-research/") else model
    box = None
    t0 = time.time()
    awake0 = time.monotonic()  # stops while the Mac sleeps, so the gap is time asleep

    def configure(hs_cfg) -> None:
        hs_cfg.project.dockerfile = str(task_dir / "environment" / "Dockerfile")
        hs_cfg.project.repo_alias = "/app"
        hs_cfg.models.box_key_placeholder = BOX_KEY_PLACEHOLDER
        hs_cfg.budget.per_session_usd = 30.0
        hs_cfg.budget.per_day_usd = 60.0
        # Flash reached the 20-minute default on 4 of 7 workers in this benchmark.
        hs_cfg.explore.worker_wall_clock_minutes = 45.0

    try:
        box = client.create_box(f"evocode-{name}"[:50], image=build_image(task_dir),
                                max_lifetime_seconds=BOX_LIFETIME, size=client.main_box_size)
        result["build_seconds"] = round(time.time() - t0)
        prepare_box(client, box.id)
        write_opencode_config(client, box.id, model)
        check_opencode(client, box.id)
        mirror_code(client, box.id, repo)
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
        subprocess.run(["git", "-c", "user.name=pilot", "-c", "user.email=p@l", "commit",
                        "-qm", "base", "--allow-empty"], cwd=repo, check=True)
        session = f"ses-evocode-{uuid.uuid4().hex[:10]}"
        result["session"] = session
        agent = BoxAgent(client, box.id, task_dir, cfg, numbers, session, model, raw_dir, repo,
                         result, out, spend)
        task = Task(id=name, title=name, test_command="", hidden_test_command="",
                    stages=[Stage(message=(task_dir / "steps" / f"round-{n}" /
                                           "instruction.md").read_text()) for n in numbers])
        episode = EvalEpisode(
            task, repo, main_agent=agent, hindsight=True, hindsight_home=out / "hs" / name,
            sail=client, session_id=session, arm="treatment_diff", relay_nudges=True,
            relay_diff=True, configure=configure, max_diff_chars=MAX_DIFF_CHARS,
            agent_has_tools=False,
            reviewer_model_fn=forced_review(force_review_at, result) if force_review_at else None,
            quiet_timeout=HINDSIGHT_FINAL_WAIT,
            settings_overrides={"stage_gap_seconds": gap, "settle_seconds": 3.0,
                                "wait_for_run": wait_for_run},
            models_override={role: bare for role in HINDSIGHT_ROLES})
        res = episode.run()
        result["hindsight"] = {"nudges": len(res.nudges), "runs": res.runs,
                               "spend_usd": res.spend_usd, "quiet_error": res.quiet_error,
                               "holds": episode.holds}
    except Exception as e:  # noqa: BLE001 - recorded, never faked
        result["error"] = f"{type(e).__name__}: {e}"[:1500]
    finally:
        result["wall_seconds"] = round(time.time() - t0)
        # The orchestrator runs here, so time asleep makes its timing meaningless.
        result["asleep_seconds"] = max(0, round((time.time() - t0) - (time.monotonic() - awake0)))
        if result["asleep_seconds"] > 60:
            print(f"{name}: the machine slept for {result['asleep_seconds']}s during this task; "
                  "don't use its timing", flush=True)
        if box is not None:
            try:
                save_checkpoints(client, box.id, out, name)
            except Exception as e:  # noqa: BLE001 - recorded, never faked
                result["error"] += f" | checkpoints not saved: {type(e).__name__}: {e}"[:300]
            try:
                client.terminate(box.id)
            except Exception as e:  # noqa: BLE001
                result["error"] += f" | terminate failed for {box.id}: {e}"
    write_result(out, result, numbers)
    return result


def write_result(out: Path, result: dict, numbers: list[int]) -> None:
    rounds = result["rounds"]
    total = len(numbers)
    stop = next((i for i, r in enumerate(rounds) if not r["passed"]), len(rounds))
    tokens = {k: sum(r.get("tokens", {}).get(k, 0) for r in rounds)
              for k in ("input", "output", "reasoning", "cache_read", "cache_write")}
    tokens_after = result.get("hindsight_turns", [])
    for k in tokens:
        tokens[k] += sum(t.get("tokens", {}).get(k, 0) for t in tokens_after)
    usd = _usd(tokens, usd_rates(result.get("model", "")))
    ratios = [r["cases_passed"] / r["cases_total"] for r in rounds if r.get("cases_total")]
    result["summary"] = {
        "rounds_total": total, "rounds_run": len(rounds),
        "rounds_passed": sum(r["passed"] for r in rounds),
        "score_continue": round(sum(r["passed"] for r in rounds) / total, 3),
        "score_fail_stop": round(stop / total, 3),
        "case_score": round(sum(ratios) / total, 3),
        "tokens": tokens, "estimated_usd_at_placeholder_prices": round(usd, 2)}
    (out / f"{result['task']}.json").write_text(json.dumps(result, indent=1))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--data", required=True, type=Path, help="unpacked task folder")
    ap.add_argument("--tasks", nargs="+", required=True, help="task names from task.toml")
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--oracle", action="store_true")
    ap.add_argument("--model", default="sail-research/zai-org/GLM-5.3")
    ap.add_argument("--max-rounds", type=int, default=None)
    ap.add_argument("--max-usd", type=float, default=100.0)
    ap.add_argument("--hindsight", action="store_true")
    ap.add_argument("--parallel", type=int, default=None,
                    help="tasks at once; Sail allows 100 live boxes per organization")
    ap.add_argument("--force-review-at", type=int,
                    help="debugging only: say yes to the review at this round")
    ap.add_argument("--force-review-middle", action="store_true",
                    help="say yes to the review at each task's middle round, rounded up")
    ap.add_argument("--no-wait", action="store_true",
                    help="send the next round on the clock, even with a redesign running")
    ap.add_argument("--box-size", choices=("s", "m", "l"), default=None,
                    help="Hindsight's boxes: 1, 4 or 8 vCPU. The organization's limit counts "
                         "reserved vCPUs, and workers use about one")
    ap.add_argument("--main-box-size", choices=("s", "m", "l"), default=None,
                    help="the main agent's box")
    ap.add_argument("--gap", type=float, default=60.0,
                    help="seconds between rounds, as a user would pause")
    args = ap.parse_args()
    if not args.oracle and not os.environ.get("SAIL_API_KEY"):
        ap.error("SAIL_API_KEY must be set")
    dirs = {}
    for d in args.data.iterdir():
        if (d / "task.toml").exists():
            dirs[task_config(d)["metadata"]["name"]] = d
    missing = [t for t in args.tasks if t not in dirs]
    if missing:
        ap.error(f"unknown tasks: {missing}")
    args.out.mkdir(parents=True, exist_ok=True)
    client = SailClient(box_policy=None if args.oracle else BOX_POLICY, box_size=args.box_size)
    client.main_box_size = args.main_box_size
    spend = Spend(args.max_usd)
    with ThreadPoolExecutor(max_workers=args.parallel or len(args.tasks)) as pool:
        if args.hindsight:
            jobs = [pool.submit(run_task_with_hindsight, client, dirs[t], model=args.model,
                                out=args.out, max_rounds=args.max_rounds, spend=spend,
                                gap=args.gap,
                                force_review_at=(middle_round(dirs[t], args.max_rounds)
                                                 if args.force_review_middle
                                                 else args.force_review_at),
                                wait_for_run=not args.no_wait)
                    for t in args.tasks]
        else:
            jobs = [pool.submit(run_task, client, dirs[t], oracle=args.oracle, model=args.model,
                                out=args.out, max_rounds=args.max_rounds, spend=spend)
                    for t in args.tasks]
        for job in as_completed(jobs):
            r = job.result()
            print(json.dumps({"task": r["task"], "error": r["error"],
                              **r.get("summary", {})}), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
