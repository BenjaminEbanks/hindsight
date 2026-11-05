#!/usr/bin/env python3
"""Hindsight in-box driver. STANDALONE: stdlib only.

The orchestrator uploads task.json, this driver, and the repo baseline,
then starts us in the background (nohup). We:

  1. run the worker harness locally (pi | opencode | codex) under a budget,
  2. stream events to /work/events.jsonl,
  3. keep /work/status.json updated (phase, turns, tokens, last test, heartbeat),
  4. enforce wall-clock budget and the early-stop rule: at half
     budget, run the tests once; if they are not passing, stop with
     exit_reason=no_progress,
  5. run the test command with per-test reporting (JUnit XML),
  6. compute the patch (git diff vs the baseline commit, new files included),
  7. write /work/result.json exactly once, then exit.

Items: the pi RPC line protocol, the opencode server HTTP
shape (V3), and `codex exec --json` output framing (V16) are written to the
documented contract and record diagnostics on mismatch instead of guessing.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tarfile
import threading
import time
import xml.etree.ElementTree as ET
from pathlib import Path

WORK = Path("/work")
REPO = Path("/repo")
STATUS_PATH = WORK / "status.json"
EVENTS_PATH = WORK / "events.jsonl"
RESULT_PATH = WORK / "result.json"
JUNIT_PATH = WORK / "junit.xml"

_MAX_EVENT_CHARS = 4000
_state_lock = threading.Lock()
_status: dict = {"exploration_id": "", "phase": "starting", "agent_turns": 0,
                "input_tokens": 0, "output_tokens": 0, "last_test": None,
                "budget": {"tokens_used_frac": 0.0, "time_used_frac": 0.0},
                "heartbeat": 0.0}


def now() -> float:
    return time.time()


def log_event(evt: dict) -> None:
    evt = {"ts": now(), **evt}
    with _state_lock:
        with open(EVENTS_PATH, "a") as f:
            f.write(json.dumps(evt) + "\n")


def set_status(**fields) -> None:
    with _state_lock:
        _status.update(fields)
        _status["heartbeat"] = now()
        tmp = STATUS_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(_status))
        os.replace(tmp, STATUS_PATH)


class Killed(Exception):
    pass


class Monitor:
    """Budget + early-stop watchdog (Sections 6.7 / 6.8)."""

    def __init__(self, task: dict, budget: dict):
        self.t0 = now()
        self.deadline = self.t0 + float(budget.get("max_wall_clock_s", 1200))
        self.early_stop_at = self.t0 + float(budget.get("max_wall_clock_s", 1200)) * float(
            budget.get("early_stop_check_frac", 0.5))
        self.test_command = task.get("test_command", "python -m pytest -q")
        self.early_stop_done = False
        frac = budget.get("wrap_up_frac")
        self.wrap_up_at = self.t0 + float(budget.get("max_wall_clock_s", 1200)) * float(frac) \
            if frac else None
        self.reason: str | None = None  # budget_time | no_progress | wrap_up
        self.proc: subprocess.Popen | None = None

    def bind(self, proc: subprocess.Popen) -> None:
        self.proc = proc

    def time_used_frac(self) -> float:
        return min(1.0, (now() - self.t0) / (self.deadline - self.t0))

    def check(self) -> None:
        """Called frequently by harness loops; raises Killed when we must stop."""
        set_status(budget={"tokens_used_frac": 0.0, "time_used_frac": self.time_used_frac()})
        if self.reason:  # the ticker stopped it; raise in the caller's thread too
            raise Killed(self.reason)
        if self.wrap_up_at and now() >= self.wrap_up_at:
            self.wrap_up_at = None
            self.reason = "wrap_up"
            self.kill()
            raise Killed("wrap_up")
        if now() >= self.deadline:
            self.reason = "budget_time"
            self.kill()
            raise Killed("budget_time")
        if not self.early_stop_done and now() >= self.early_stop_at:
            self.early_stop_done = True
            if not self.test_command.strip():
                return  # no tests yet is not a lack of progress
            res = run_tests(self.test_command)
            set_status(last_test={"passed": res["passed"], "failed": res["failed"],
                                  "at_turn": _status.get("agent_turns", 0)},
                       phase="early_stop_check")
            if res["failed"] > 0 or res["passed"] == 0:
                self.reason = "no_progress"
                self.kill()
                raise Killed("no_progress")

    def kill(self) -> None:
        if self.proc is not None:
            try:
                self.proc.kill()
            except Exception:
                pass


# -------------------------------------------------------------------- tests

def run_tests(test_command: str) -> dict:
    """Run the test command with JUnit XML per-test reporting."""
    cmd = test_command
    if not cmd.strip():  # nothing to run is not a pass
        return {"passed": 0, "failed": 0, "tests": [], "stdout": ""}
    junit = str(JUNIT_PATH)
    if "pytest" in cmd and "--junitxml" not in cmd:
        cmd = f"{cmd} --junitxml={junit}"
    set_status(phase="testing")
    try:
        proc = subprocess.run(cmd, shell=True, cwd=str(REPO), capture_output=True, timeout=900)
    except subprocess.TimeoutExpired:
        return {"passed": 0, "failed": 1, "tests": [], "stdout": "test command timed out"}
    passed, failed, names = 0, 0, []
    if junit and Path(junit).exists():
        try:
            tree = ET.parse(junit)
            for tc in tree.iter("testcase"):
                name = tc.get("classname", "") + "::" + tc.get("name", "")
                names.append(name)
                failed_el = list(tc)
                if any(el.tag in ("failure", "error") for el in failed_el):
                    failed += 1
                else:
                    passed += 1
        except ET.ParseError:
            passed, failed = 0, 1
    else:
        # Fallback: exit code only; evidence is coarse.
        failed = 0 if proc.returncode == 0 else 1
        passed = 0 if proc.returncode != 0 else 1
    out = proc.stdout.decode(errors="replace")[-8000:] if proc.stdout else ""
    return {"passed": passed, "failed": failed, "tests": names, "stdout": out}


# -------------------------------------------------------------------- patch

def write_preimages(baseline: str) -> None:
    """Baseline (pre-image) copies of every changed path -> /work/preimages.tar.gz.

    The rebase/matrix boxes on the orchestrator side are FRESH git repos
    containing only the latest snapshot: they lack the blob objects this
    patch's hunks were computed against, so `git apply --3way` cannot do a
    real merge and falls back to literal context matching — any drift in the
    same file (the user/main agent keeps coding while workers run) records
    a fake "conflict" and the proposal is discarded .
    Shipping the pre-image blobs lets the other side `git hash-object -w`
    them, giving --3way the ancestor it needs for a genuine 3-way merge.
    New (untracked-at-baseline) files have no pre-image and are skipped.
    """
    def git(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run(["git", "-C", str(REPO), *args], capture_output=True)

    names = [n for n in git("diff", "--name-only", baseline)
             .stdout.decode(errors="replace").splitlines() if n]
    pre_dir = WORK / "preimages"
    if pre_dir.exists():
        shutil.rmtree(pre_dir)
    pre_dir.mkdir(parents=True)
    written = 0
    for name in names:
        if git("cat-file", "-e", f"{baseline}:{name}").returncode != 0:
            continue  # added file: no pre-image exists
        blob = subprocess.run(
            ["git", "-C", str(REPO), "show", f"{baseline}:{name}"],
            capture_output=True).stdout
        dst = pre_dir / name
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(blob)
        written += 1
    with tarfile.open(WORK / "preimages.tar.gz", "w:gz") as tar:
        if written:
            tar.add(pre_dir, arcname="preimages")
    shutil.rmtree(pre_dir, ignore_errors=True)


def compute_patch(baseline: str) -> tuple[str, dict]:
    def git(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run(["git", "-C", str(REPO), *args], capture_output=True)

    # Exclude bytecode from the sweep: `git add -A` staged
    # untracked __pycache__/*.pyc into every patch (seen live in the
    # worker-box verification), inflating diff stats and shipping binary
    # noise into selection boxes. The harness's own edge tests still ride
    # along by design; the selection-side product-code gate filters those.
    # Installed dependencies stay out too: one worker's patch was 5.8 MB of .venv.
    git("add", "-A", "--", ":(exclude)__pycache__", ":(exclude)*.pyc",
        ":(exclude,glob)**/.venv/**", ":(exclude,glob)**/venv/**",
        ":(exclude,glob)**/node_modules/**")
    # Binaries stay out. Plain `git diff` writes "Binary files differ", which
    # `git apply` refuses, so one compiled program in the repo lost the whole design
    # (seen live on EvoCode). It also passed as "changed product code".
    stat = git("diff", "--numstat", baseline).stdout.decode(errors="replace")
    keep = ["--", "."] + [f":(exclude,literal){line.split(chr(9), 2)[2]}"
                          for line in stat.splitlines() if line.startswith("-\t-\t")]
    proc = git("diff", baseline, *keep)
    patch = proc.stdout.decode(errors="replace")
    stat = git("diff", "--numstat", baseline, *keep).stdout.decode(errors="replace")
    files = added = removed = 0
    for line in stat.splitlines():
        parts = line.split("\t")
        if len(parts) == 3:
            files += 1
            added += int(parts[0]) if parts[0].isdigit() else 0
            removed += int(parts[1]) if parts[1].isdigit() else 0
    return patch, {"files": files, "added": added, "removed": removed}


# ----------------------------------------------------------- summary parsing

def parse_summary_block(text: str) -> dict | None:
    """Parse the worker's final ```json { ... } ``` block."""
    matches = re.findall(r"```json\s*(\{.*?\})\s*```", text, re.DOTALL)
    for m in reversed(matches):
        try:
            obj = json.loads(m)
            if "summary" in obj:
                return obj
        except json.JSONDecodeError:
            continue
    return None


# ------------------------------------------------------------------ harnesses

def harness_pi(task: dict, monitor: Monitor) -> dict:
    """Pi via RPC mode: one prompt command, read JSON events until the final
    agent_end. Final-signal shape per pinned version."""
    model = task.get("model", "")
    prompt = task["prompt"]
    proc = subprocess.Popen(
        ["pi", "--mode", "rpc", "--no-session"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        cwd=str(REPO), text=True, bufsize=1,
    )
    monitor.bind(proc)
    log_event({"type": "harness_start", "harness": "pi", "model": model})
    assert proc.stdin is not None
    proc.stdin.write(json.dumps({"cmd": "prompt", "text": prompt}) + "\n")
    proc.stdin.flush()

    final_text, turns, in_tok, out_tok = [], 0, 0, 0
    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            monitor.check()
            line = line.strip()
            if not line:
                continue
            try:
                evt = json.loads(line)
            except json.JSONDecodeError:
                log_event({"type": "harness_raw", "line": line[:500]})
                continue
            kind = evt.get("type") or evt.get("event") or ""
            log_event({"type": "harness_event", "kind": kind})
            if kind in ("agent_end", "result", "done", "end"):
                for key in ("text", "message", "result", "output"):
                    if isinstance(evt.get(key), str):
                        final_text.append(evt[key])
                turns += 1
                break
            if kind in ("assistant", "message", "agent_message"):
                turns += 1
                for key in ("text", "message", "content"):
                    v = evt.get(key)
                    if isinstance(v, str):
                        final_text.append(v)
            usage = evt.get("usage") or {}
            if isinstance(usage, dict):
                in_tok += int(usage.get("input_tokens", 0) or 0)
                out_tok += int(usage.get("output_tokens", 0) or 0)
            set_status(agent_turns=turns, input_tokens=in_tok, output_tokens=out_tok)
        monitor.check()
    except Killed:
        pass
    text = "\n".join(final_text)
    return {"exit_reason": "done", "agent_turns": turns, "input_tokens": in_tok,
            "output_tokens": out_tok, "final_text": text,
            "stderr_tail": (proc.stderr.read()[-2000:] if proc.stderr else "")}


def harness_opencode(task: dict, monitor: Monitor) -> dict:
    """Worker agent via the opencode CLI: `opencode run --standalone
    --auto --format json` — the SAME invocation the eval's main agents
    ran all night (hindsight/eval/harness.py: JSONL event
    stream; errors still exit 0 and must be checked explicitly).

    Replaces the `opencode serve` + HTTP experiment, which was NEVER
    exercised live before and failed on contact: v2.2.5 has
    no POST /session (405), the real API lives under /api/*, and its
    password auth scheme was not resolvable from the binary (every
    header variant 401). The CLI path is proven; use it.
    """
    # Preflight + config bootstrap: a missing CLI or config
    # previously surfaced only as a silent empty patch (the `pi` default
    # shipped NO agent CLI at all — every worker errored and the pipeline
    # still 'delivered' its empty patch as a redesign). Fail loudly here.
    if shutil.which("opencode") is None:
        raise RuntimeError(
            "opencode CLI not found in worker box — the env checkpoint "
            "must install it (env.HARNESS_INSTALL)")
    model = task.get("model", "")
    if "/" not in model:
        raise RuntimeError(
            f"worker task model {model!r} is not provider-qualified "
            f"(expected '<provider>/<model>')")
    cfg_dir = Path.home() / ".config" / "opencode"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    cfg_path = cfg_dir / "opencode.json"
    if not cfg_path.exists():
        cfg_path.write_text(json.dumps({
            "$schema": "https://opencode.ai/config.json",
            "provider": {
                task["model"].split("/")[0]: {
                    "npm": "@ai-sdk/openai-compatible",
                    "name": task["model"].split("/")[0],
                    "options": {
                        "baseURL": task.get("base_url",
                                            "https://api.sailresearch.com/v1"),
                        "apiKey": task.get("api_key", ""),
                        "timeout": False,
                    },
                    "models": {
                        task["model"].split("/", 1)[1]:
                            {"name": task["model"].split("/", 1)[1]},
                    },
                },
            },
        }))

    # the CLI resolves its
    # project directory from $PWD, NOT the process cwd — PWD must point
    # at the repo or the agent edits the wrong tree.
    # `agent_session` continues an earlier conversation in this box.
    session_id = (task.get("agent_session")
                  or f"ses-hs-worker-{task.get('exploration_id', 'x')}"[:60])
    # By full path when present: some images carry another opencode on the PATH.
    binary = "/opt/oc/opencode" if Path("/opt/oc/opencode").exists() else "opencode"
    cmd = [binary, "run", "--standalone", "--session", session_id,
           "--auto", "--format", "json", "--model", model, task["prompt"]]
    env = dict(os.environ)
    env["PWD"] = str(REPO)
    proc = subprocess.Popen(
        cmd, cwd=str(REPO), env=env,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1,
    )
    monitor.bind(proc)
    log_event({"type": "harness_start", "harness": "opencode", "model": model})

    # Budget ticker: monitor.check() from the stdout loop alone would
    # miss a runaway agent that emits nothing for minutes. A daemon
    # thread enforces the deadline; kill() closes stdout and unblocks
    # the reader, and the post-loop check() re-raises Killed in THIS
    # thread (the exception main() expects).
    def _tick() -> None:
        while proc.poll() is None:
            time.sleep(5)
            try:
                monitor.check()
            except Killed:
                return

    threading.Thread(target=_tick, daemon=True).start()

    texts: list[str] = []
    errors: list[str] = []
    tokens = {"input": 0, "output": 0, "reasoning": 0, "cache": 0}
    turns = 0
    assert proc.stdout is not None
    for line in proc.stdout:
        monitor.check()
        line = line.strip()
        if not line:
            continue
        try:
            data = json.loads(line)
        except ValueError:
            continue
        if not isinstance(data, dict):
            continue
        # What the worker did, for the trace. Long tool output is cut.
        log_event({"type": "agent", "raw": line[:_MAX_EVENT_CHARS]})
        if data.get("type") == "error":
            err = data.get("error") or {}
            errors.append(str(err.get("message") or data))
            continue
        if data.get("type") == "text":
            part = data.get("part") or {}
            if isinstance(part.get("text"), str):
                texts.append(part["text"])
                turns += 1
        part = data.get("part") or {}
        if isinstance(part, dict) and part.get("type") == "step-finish":
            t = part.get("tokens") or {}
            for k in ("input", "output", "reasoning"):
                tokens[k] += int(t.get(k) or 0)
            cache = t.get("cache") or {}
            tokens["cache"] += int(cache.get("read") or 0) + int(cache.get("write") or 0)
            # Kept in the status so a worker killed by its time limit is still billed.
            set_status(input_tokens=tokens["input"] + tokens["cache"],
                       output_tokens=tokens["output"] + tokens["reasoning"])
        set_status(agent_turns=turns)
    monitor.check()  # re-raise Killed here if the ticker fired
    rc = proc.wait()
    if errors or rc != 0:
        stderr_tail = (proc.stderr.read() if proc.stderr else "") or ""
        raise RuntimeError(
            f"opencode run failed (exit {rc}): "
            f"{'; '.join(errors[-3:]) or stderr_tail[-500:]}")
    return {"exit_reason": "done", "agent_turns": max(turns, 1),
            "input_tokens": tokens["input"] + tokens["cache"],
            "output_tokens": tokens["output"] + tokens["reasoning"],
            "final_text": "\n".join(texts), "stderr_tail": ""}


def harness_codex(task: dict, monitor: Monitor) -> dict:
    """Codex: `codex exec --json` streaming NDJSON.
    Responses API through the Sail custom provider."""
    proc = subprocess.Popen(
        ["codex", "exec", "--json", "-C", str(REPO), task["prompt"]],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1,
    )
    monitor.bind(proc)
    log_event({"type": "harness_start", "harness": "codex", "model": task.get("model", "")})
    turns, in_tok, out_tok = 0, 0, 0
    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            monitor.check()
            line = line.strip()
            if not line:
                continue
            try:
                evt = json.loads(line)
            except json.JSONDecodeError:
                log_event({"type": "harness_raw", "line": line[:500]})
                continue
            kind = evt.get("type", "")
            log_event({"type": "harness_event", "kind": kind})
            turns += 1 if kind in ("agent_message", "assistant") else 0
            usage = evt.get("usage") or evt.get("token_count") or {}
            if isinstance(usage, dict):
                in_tok += int(usage.get("input_tokens", usage.get("input", 0)) or 0)
                out_tok += int(usage.get("output_tokens", usage.get("output", 0)) or 0)
            set_status(agent_turns=turns, input_tokens=in_tok, output_tokens=out_tok)
        monitor.check()
    except Killed:
        pass
    return {"exit_reason": "done", "agent_turns": turns, "input_tokens": in_tok,
            "output_tokens": out_tok, "final_text": "", "stderr_tail": ""}


HARNESSES = {"pi": harness_pi, "opencode": harness_opencode, "codex": harness_codex}


# --------------------------------------------------------------------- main

def run_answer_mode(task: dict) -> int:
    """The main agent's question for the finished worker. Its box was woken, or
    revived from a checkpoint, so /repo and /work/result.json are as it left them.
    One short agent turn answers from the worker's own context."""
    question = str(task.get("question") or "")
    result = {}
    try:
        result = json.loads((WORK / "result.json").read_text())
    except Exception:  # noqa: BLE001 - resume must not crash on a missing file
        pass
    prompt = (
        "You are the background engineer who produced the redesign in /repo "
        "(your patch is /work/patch.diff; your own summary of it is below).\n\n"
        "YOUR SUMMARY:\n" + json.dumps(result.get("summary") or {}, indent=2)[:4000]
        + "\n\nA question about your redesign:\n" + question
        + "\n\nAnswer from your own work and context, concisely. If you are "
          "uncertain, say so plainly — never guess. Plain text only."
    )
    task = {**task, "prompt": prompt}
    # No early-stop test run in answer mode (frac > 1 disables it); the
    # wall clock still bounds the turn.
    monitor = Monitor(task, {"max_wall_clock_s": task.get("budget", {}).get(
        "max_wall_clock_s", 300), "early_stop_check_frac": 2.0})
    harness_fn = HARNESSES.get(task.get("harness", "opencode"))
    answer, exit_reason, usage = "", "error", {"agent_turns": 0,
                                                "input_tokens": 0,
                                                "output_tokens": 0}
    if harness_fn is None:
        log_event({"type": "error", "error": f"unknown harness {task.get('harness')}"})
    else:
        set_status(phase="answering")
        try:
            run = harness_fn(task, monitor)
            answer = run.get("final_text", "")
            exit_reason = run.get("exit_reason", "done")
            usage = {k: run.get(k, 0) for k in
                     ("agent_turns", "input_tokens", "output_tokens")}
        except Exception as e:  # noqa: BLE001 - record, never fake a result
            log_event({"type": "harness_error", "error": repr(e)})
    (WORK / "answer.json").write_text(json.dumps({
        "question": question,
        "answer": answer,
        "exit_reason": exit_reason,
        **usage,
    }))
    set_status(phase="answer_finished", exit_reason=exit_reason)
    log_event({"type": "answer_done", "exit_reason": exit_reason})
    return 0


WRAP_UP_PROMPT = (
    "You are running out of time. Stop starting new work. Get what you have working, run "
    "the tests, read /work/updates if you haven't, and finish now with the JSON summary "
    "block. Say in it what you didn't get to.")
RECONNECTED_PROMPT = (
    "The connection dropped, and your work is still in /repo. Carry on from where you "
    "stopped, and finish with the JSON summary block as asked.")
_DROPPED_CONNECTION = re.compile(
    r"ECONNRESET|ETIMEDOUT|ECONNREFUSED|EPIPE|socket connection was closed|socket hang up"
    r"|fetch failed", re.I)
_MAX_RECONNECTS = 3


def run_agent(harness_fn, task: dict, monitor: Monitor) -> dict:
    """Runs the agent to its end. A dropped connection continues the conversation, and so
    does the time to wrap up. Out of time returns what was spent, and the code stays."""
    continues = task.get("harness", "opencode") == "opencode"
    spent = {"input_tokens": 0, "output_tokens": 0}
    drops = 0

    def add_spent() -> None:
        for key in spent:
            spent[key] += _status.get(key, 0)
        set_status(input_tokens=0, output_tokens=0)

    while True:
        try:
            run = harness_fn(task, monitor)
            for key in spent:
                run[key] = run.get(key, 0) + spent[key]
            return run
        except Killed:
            add_spent()
            if monitor.reason != "wrap_up" or not continues:
                return {"exit_reason": monitor.reason or "budget_time", "agent_turns": 0,
                        **spent, "final_text": "", "stderr_tail": ""}
            monitor.reason = None
            task = {**task, "prompt": WRAP_UP_PROMPT}
        except RuntimeError as e:
            if not (continues and _DROPPED_CONNECTION.search(str(e))
                    and drops < _MAX_RECONNECTS):
                raise
            add_spent()
            drops += 1
            log_event({"type": "reconnect", "attempt": drops, "error": repr(e)[:500]})
            time.sleep(30 * drops)
            task = {**task, "prompt": RECONNECTED_PROMPT}


def main() -> int:
    task = json.loads((WORK / "task.json").read_text())
    if task.get("mode") == "answer":
        return run_answer_mode(task)
    budget = task.get("budget", {})
    _status["exploration_id"] = task["exploration_id"]
    set_status(phase="running")

    monitor = Monitor(task, budget)
    harness_fn = HARNESSES.get(task.get("harness", "opencode"))
    exit_reason, run = "error", None
    if harness_fn is None:
        log_event({"type": "error", "error": f"unknown harness {task.get('harness')}"})
    else:
        try:
            run = run_agent(harness_fn, task, monitor)
        except Exception as e:  # noqa: BLE001 - record, never fake a result
            log_event({"type": "harness_error", "error": repr(e)})
            run = {"exit_reason": "error", "agent_turns": 0, "input_tokens": 0,
                   "output_tokens": 0, "final_text": "", "stderr_tail": repr(e)}
        exit_reason = run["exit_reason"]

    summary = parse_summary_block(run.get("final_text", "")) if run else None
    if summary is None and exit_reason == "done":
        exit_reason = "error"  # missing/invalid summary block

    tests = run_tests(task.get("test_command", "python -m pytest -q")
                      or (summary or {}).get("verify_command") or "")
    baseline = task.get("baseline_commit") or "HEAD"
    patch, diff_stat = compute_patch(baseline)
    write_preimages(baseline)

    result = {
        "exploration_id": task["exploration_id"],
        "harness": task.get("harness", "pi"),
        "exit_reason": exit_reason,
        "agent_turns": run.get("agent_turns", 0) if run else 0,
        "input_tokens": run.get("input_tokens", 0) if run else 0,
        "output_tokens": run.get("output_tokens", 0) if run else 0,
        "tests": {"command": task.get("test_command", ""),
                  "passed": tests["passed"], "failed": tests["failed"],
                  "names": tests["tests"], "junit_path": str(JUNIT_PATH)},
        "patch_path": "/work/patch.diff",
        "diff_stat": diff_stat,
        "summary": summary,
        "transcript_path": str(EVENTS_PATH),
    }
    (WORK / "patch.diff").write_text(patch)
    RESULT_PATH.write_text(json.dumps(result))
    set_status(phase="finished", exit_reason=exit_reason)
    log_event({"type": "driver_done", "exit_reason": exit_reason})
    return 0


if __name__ == "__main__":
    sys.exit(main())
