"""OpenCodeAgent reply parsing — pinned to the REAL verified CLI shape
(docs/VERIFY.md). Not a guess: these are the actual
event lines the bundled CLI emitted."""

from __future__ import annotations

import pytest

from hindsight.eval.harness import _extract_reply, _scan_events

# Actual events from `opencode run --format json` (V-E1 probe).
REAL_STREAM = "\n".join([
    '{"type":"step_start","timestamp":1793490536904,"sessionID":"ses-x",'
    '"part":{"id":"prt_a","sessionID":"ses-x","messageID":"msg_1",'
    '"type":"step-start"}}',
    '{"type":"text","timestamp":1793490537204,"sessionID":"ses-x",'
    '"part":{"id":"prt_b","sessionID":"ses-x","messageID":"msg_1",'
    '"type":"text","text":"OK","time":{"start":1,"end":2}}}',
    '{"type":"step_finish","timestamp":1793490537204,"sessionID":"ses-x",'
    '"part":{"id":"prt_c","sessionID":"ses-x","messageID":"msg_1",'
    '"type":"step-finish","reason":"stop","cost":0,'
    '"tokens":{"input":7620,"output":2,"reasoning":24,'
    '"cache":{"read":0,"write":0}}}}',
])

# Actual error event: exit code was STILL 0 (V-E1).
REAL_ERROR = ('{"type":"error","timestamp":1793490515148,'
              '"sessionID":"ses-y","error":{"type":"provider.free-limit",'
              '"message":"Free usage limit reached.","status":429}}')

MULTI_TEXT = "\n".join([
    '{"type":"text","part":{"type":"text","text":"part one "}}',
    '{"type":"text","part":{"type":"text","text":"part two"}}',
])


def test_extract_reply_from_real_stream():
    assert _extract_reply(REAL_STREAM) == "OK"


def test_extract_reply_joins_multiple_text_parts():
    assert _extract_reply(MULTI_TEXT) == "part one \npart two"


def test_extract_reply_falls_back_to_plain_text():
    assert _extract_reply("just some output") == "just some output"


def test_scan_events_collects_tokens():
    errors, tokens = _scan_events(REAL_STREAM)
    assert errors == []
    assert tokens == {"input": 7620, "output": 2, "reasoning": 24}


def test_scan_events_detects_error_that_exits_zero():
    errors, tokens = _scan_events(REAL_ERROR)
    assert errors == ["Free usage limit reached."]
    assert tokens == {"input": 0, "output": 0, "reasoning": 0}


def test_opencode_agent_raises_on_error_event(tmp_path, monkeypatch):
    """The agent must not silently treat an error stream as a reply —
    that would fake an episode result. (Retries are skipped-in-time here:
    the backoff sleeps are patched out.)"""
    from hindsight.eval import harness as h
    from hindsight.eval.harness import OpenCodeAgent

    class FakeCompleted:
        returncode = 0
        stdout = REAL_ERROR
        stderr = ""

    monkeypatch.setattr("subprocess.run", lambda *a, **k: FakeCompleted())
    monkeypatch.setattr(h.time, "sleep", lambda s: None)
    agent = OpenCodeAgent(tmp_path)
    with pytest.raises(RuntimeError, match="after 3 attempts"):
        agent.send("hello")


def test_opencode_agent_sets_pwd_to_repo(tmp_path, monkeypatch):
    """The CLI resolves its project directory
    from the inherited $PWD env var, not the process cwd: without this,
    an agent spawned from live_eval.py (cwd = the outer project) edits
    the OUTER project even with subprocess cwd = the staged repo. The
    harness must override PWD on every invocation."""
    from hindsight.eval.harness import OpenCodeAgent

    captured: dict = {}

    class FakeCompleted:
        returncode = 0
        stdout = REAL_STREAM
        stderr = ""

    def fake_run(*a, **k):
        captured.update(k)
        return FakeCompleted()

    monkeypatch.setenv("PWD", "/some/other/project")
    monkeypatch.setattr("subprocess.run", fake_run)
    agent = OpenCodeAgent(tmp_path)
    agent.send("hello")
    assert captured["cwd"] == str(tmp_path)
    assert captured["env"]["PWD"] == str(tmp_path)
