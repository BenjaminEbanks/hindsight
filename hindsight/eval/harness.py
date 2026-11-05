"""Main-agent harnesses for eval episodes.

The eval replaces "the user's OpenCode session" with a harness that
receives user messages and edits the repo. Two implementations:

  - ScriptedAgent: deterministic canned replies + file edits (UNIT TESTS
    and reproducible replay; clearly labeled).
  - OpenCodeAgent: the real thing — local `opencode run` in the staged
    repo, one session per episode, permissions auto-approved so the agent
    can actually edit files. Flag surface verified against
    the bundled CLI; the --format json reply shape is parsed defensively
    and confirmed at live-run time.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
import uuid
from pathlib import Path
from typing import Protocol

_APP_CLI = "/Applications/OpenCode.app/Contents/Resources/opencode-cli"
_AGENT_TIMEOUT_SECONDS = 30 * 60


class MainAgentHarness(Protocol):
    def send(self, message: str) -> str: ...

    def close(self) -> None: ...


class ScriptedAgent:
    """Deterministic main agent (UNIT TEST MOCK / replay). Applies the
    canned file edits and returns the canned reply for each turn. It never
    improvises: running out of script is an error, not a guess."""

    def __init__(self, repo_dir: str | Path,
                 script: list[dict]):
        self.repo_dir = Path(repo_dir)
        self.script = list(script)
        self.turns_sent = 0
        self.messages: list[str] = []

    def send(self, message: str) -> str:
        if not self.script:
            raise RuntimeError(
                "ScriptedAgent ran out of script — the episode sent more "
                "user turns than were scripted")
        step = self.script.pop(0)
        self.turns_sent += 1
        self.messages.append(message)
        for rel, content in (step.get("files") or {}).items():
            f = self.repo_dir / rel
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(content)
        return step.get("reply") or "(scripted reply)"

    def close(self) -> None:
        pass


def _cli_path() -> str:
    found = shutil.which("opencode")
    if found:
        return found
    if Path(_APP_CLI).exists():
        return _APP_CLI
    raise RuntimeError(
        "no opencode CLI found (looked on PATH and at "
        f"{_APP_CLI}); the live eval needs it")


def _extract_reply(raw: str) -> str:
    """`opencode run --format json` emits a JSONL event
    stream: one JSON object per line. Assistant text arrives as events of
    type "text" whose part.text holds the content. Errors arrive as
    {"type":"error",...} and STILL exit 0 — callers must check for them."""
    text = raw.strip()
    if not text:
        return ""
    # JSONL event stream (the real shape).
    texts: list[str] = []
    saw_json = False
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            data = json.loads(line)
        except ValueError:
            continue
        saw_json = True
        if isinstance(data, dict) and data.get("type") == "text":
            part = data.get("part") or {}
            if isinstance(part.get("text"), str):
                texts.append(part["text"])
    if texts:
        return "\n".join(texts)
    # Defensive fallbacks for a single JSON object of assorted shapes.
    if saw_json:
        try:
            data = json.loads(text)
        except ValueError:
            return text
        if isinstance(data, dict):
            for key in ("text", "message", "content", "output"):
                v = data.get(key)
                if isinstance(v, str) and v.strip():
                    return v
    return text


def _scan_events(raw: str) -> tuple[list[str], dict]:
    """Collect text parts and error events from a JSONL run stream.
    Returns (error_messages, token_totals)."""
    errors: list[str] = []
    tokens = {"input": 0, "output": 0, "reasoning": 0}
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            data = json.loads(line)
        except ValueError:
            continue
        if not isinstance(data, dict):
            continue
        if data.get("type") == "error":
            err = data.get("error") or {}
            errors.append(str(err.get("message") or data))
        part = data.get("part") or {}
        if isinstance(part, dict) and part.get("type") == "step-finish":
            t = part.get("tokens") or {}
            for k in tokens:
                tokens[k] += int(t.get(k) or 0)
    return errors, tokens


class OpenCodeAgent:
    """The real main agent: local OpenCode CLI in the staged repo."""

    def __init__(self, repo_dir: str | Path, *, model: str | None = None,
                 timeout: int = _AGENT_TIMEOUT_SECONDS):
        self.repo_dir = Path(repo_dir)
        self.model = model
        self.timeout = timeout
        # session ids must start with "ses".
        self.session_id = f"ses-hs-eval-{uuid.uuid4().hex[:10]}"
        self.raw_replies: list[str] = []
        # Written out with the episode: without these a failure that never
        # produced the module cannot be diagnosed, adoption cannot be read,
        # and the main agent's cost looks like $0.
        self.turn_log: list[dict] = []
        self.token_totals = {"input": 0, "output": 0, "reasoning": 0}
        self.errors: list[str] = []

    def send(self, message: str) -> str:
        # --standalone: a private server for this invocation. Without it
        # the CLI talks to the background service, whose working directory
        # is wherever that service was started (here: the eval project
        # itself) — the agent then edits THAT project, not the staged repo
        # (found live twice, despite the subprocess cwd).
        cmd = [_cli_path(), "run", "--standalone",
               "--session", self.session_id,
               "--auto", "--format", "json"]
        if self.model:
            cmd += ["--model", self.model]
        cmd += [message]
        # Transient provider failures ("Timed out waiting for response
        # data", 429 rate limits) killed a multi-hour live run once —
        # retry with backoff instead. Retries are recorded, never hidden.
        # The CLI resolves its project
        # directory from the inherited $PWD env var, NOT from the process
        # cwd: subprocess.run(cwd=staged) alone leaves $PWD pointing at
        # wherever the parent process started, and the agent then edits
        # THAT directory (the outer project) while reading its files.
        # Setting PWD to the repo binds it correctly (probe-verified both
        # by setting PWD and by unsetting it; git init + --standalone
        # were necessary but not sufficient).
        env = dict(os.environ)
        env["PWD"] = str(self.repo_dir)
        last_error = ""
        for attempt in range(3):
            r = subprocess.run(cmd, cwd=str(self.repo_dir),
                               capture_output=True, text=True,
                               timeout=self.timeout, env=env)
            raw = r.stdout or ""
            errors, tokens = _scan_events(raw)
            for k in self.token_totals:
                self.token_totals[k] += tokens[k]
            self.turn_log.append({
                "message": message, "attempt": attempt, "exit": r.returncode,
                "errors": errors, "tokens": tokens, "raw": raw})
            if r.returncode == 0 and not errors:
                self.raw_replies.append(raw)
                return _extract_reply(raw)
            last_error = "; ".join(errors[-3:]) or \
                (r.stderr or r.stdout)[-1000:]
            if attempt < 2:
                time.sleep(30 * (attempt + 1))
        # error events exit 0 — detected explicitly above,
        # because a silent error reply would fake an episode result.
        raise RuntimeError(
            f"opencode run failed after {attempt + 1} attempts "
            f"(exit {r.returncode}): {last_error}")

    def close(self) -> None:
        pass
