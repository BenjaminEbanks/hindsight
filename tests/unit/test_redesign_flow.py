"""The final worker continues the chosen design, critics read whole designs, boxes never
hold the key, and workers survive dropped connections and time limits. Mocks are unit-test
fakes, labelled, and never enter evaluation outputs."""

from __future__ import annotations

import json

import pytest

from hindsight.box import driver as drv
from hindsight.config import HindsightConfig
from hindsight.council import Council, proposal_brief
from hindsight.eval.episode import EvalEpisode
from hindsight.selection import product_patch

PRODUCT = "diff --git a/app/core.py b/app/core.py\n--- a/app/core.py\n+++ b/app/core.py\n+x = 1\n"
TESTS = "diff --git a/tests/test_core.py b/tests/test_core.py\n+++ b/tests/test_core.py\n+def t(): pass\n"
META = "diff --git a/pyproject.toml b/pyproject.toml\n+++ b/pyproject.toml\n+name = 1\n"


def test_product_patch_leaves_out_tests_and_metadata():
    assert product_patch(TESTS + PRODUCT + META) == PRODUCT


def test_critics_read_a_whole_design_without_its_tests():
    long_design = PRODUCT + "+" + "y" * 60_000 + "\n"
    brief = proposal_brief("A", {"patch": TESTS + long_design})
    assert "y" * 60_000 in brief
    assert "test_core" not in brief


def test_a_design_past_the_limit_says_what_was_cut():
    brief = proposal_brief("A", {"patch": PRODUCT}, max_chars=10)
    assert "more characters not shown" in brief


def _chairman(raw):
    cfg = HindsightConfig.__new__(HindsightConfig)
    council = Council.__new__(Council)
    council.cfg = type("C", (), {"project": type("P", (), {"test_command": "t"})})()
    council.model_fn = lambda prompt: raw
    labels = {"A": "p1", "B": "p2"}
    return council.chairman_directive(
        "r", "words", [{"id": "p1"}, {"id": "p2"}],
        [{"reviewer": "x", "raw": "", "ranking": ["A", "B"]}], labels, "the code")


def test_chairman_names_the_design_to_continue():
    got = _chairman("why\n###BASE: Proposal B\n###DIRECTIVE\nthe plan")
    assert got["chosen_proposal_id"] == "p2"
    assert got["directive"] == "the plan" and "###BASE" not in got["rationale"]


def test_a_chairman_naming_none_falls_back_to_the_critics_first_choice():
    assert _chairman("why\n###DIRECTIVE\nplan")["chosen_proposal_id"] == "p1"
    assert _chairman("###BASE: Proposal Z\n###DIRECTIVE\nplan")["chosen_proposal_id"] == "p1"


def test_boxes_are_given_the_placeholder_never_the_key(monkeypatch):
    from hindsight.config import ModelsConfig
    from hindsight.executor import Executor

    monkeypatch.setenv("SAIL_API_KEY", "real-secret-value")
    models = ModelsConfig()
    assert models.box_policy and models.box_key_placeholder

    class Store:
        def kv_set(self, *a): pass
        def last_user_message_id(self, *a): return 0
        def user_text_for_session(self, *a): return ["hi"]

    ex = Executor.__new__(Executor)
    ex.store = Store()
    ex.cfg = type("C", (), {
        "project": type("P", (), {"test_command": "t"}),
        "models": models,
        "explore": type("E", (), {"worker_wall_clock_minutes": 45, "worker_max_agent_turns": 1})})()
    plan = type("Plan", (), {"strategy": "open_redesign"})()
    task = ex._task_json("ex", plan, "opencode", "base", "s")
    assert "real-secret-value" not in json.dumps(task)
    assert task["api_key"] == models.box_key_placeholder


class Harness:
    """UNIT TEST MOCK: scripted agent runs."""

    def __init__(self, script):
        self.script = list(script)
        self.prompts = []

    def __call__(self, task, monitor):
        self.prompts.append(task["prompt"])
        step = self.script.pop(0)
        if isinstance(step, BaseException):
            drv.set_status(input_tokens=7, output_tokens=3)
            if isinstance(step, drv.Killed):
                monitor.reason = str(step)
            raise step
        return {"exit_reason": "done", "input_tokens": 1, "output_tokens": 1, "final_text": step}


@pytest.fixture
def box(tmp_path, monkeypatch):
    monkeypatch.setattr(drv, "STATUS_PATH", tmp_path / "status.json", raising=False)
    monkeypatch.setattr(drv, "EVENTS_PATH", tmp_path / "events.jsonl", raising=False)
    monkeypatch.setattr(drv.time, "sleep", lambda s: None)


def _monitor():
    return drv.Monitor({"test_command": ""}, {"max_wall_clock_s": 100})


def test_a_dropped_connection_continues_the_conversation(box):
    h = Harness([RuntimeError("Transport: ECONNRESET: socket closed"), "finished"])
    run = drv.run_agent(h, {"prompt": "work", "harness": "opencode"}, _monitor())
    assert run["final_text"] == "finished"
    assert h.prompts == ["work", drv.RECONNECTED_PROMPT]
    assert run["input_tokens"] == 8 and run["output_tokens"] == 4  # both attempts billed


def test_reconnecting_gives_up_eventually(box):
    h = Harness([RuntimeError("ECONNRESET")] * 9)
    with pytest.raises(RuntimeError):
        drv.run_agent(h, {"prompt": "work", "harness": "opencode"}, _monitor())
    assert len(h.prompts) == drv._MAX_RECONNECTS + 1


def test_other_errors_are_not_retried(box):
    h = Harness([RuntimeError("bad request")])
    with pytest.raises(RuntimeError):
        drv.run_agent(h, {"prompt": "work", "harness": "opencode"}, _monitor())
    assert len(h.prompts) == 1


def test_wrap_up_continues_so_the_worker_can_describe_its_work(box):
    h = Harness([drv.Killed("wrap_up"), "summary"])
    run = drv.run_agent(h, {"prompt": "work", "harness": "opencode"}, _monitor())
    assert run["exit_reason"] == "done" and h.prompts[1] == drv.WRAP_UP_PROMPT


def test_out_of_time_returns_with_the_reason(box):
    h = Harness([drv.Killed("budget_time")])
    run = drv.run_agent(h, {"prompt": "work", "harness": "opencode"}, _monitor())
    assert run["exit_reason"] == "budget_time" and run["input_tokens"] == 7


def test_monitor_asks_to_wrap_up_once_then_enforces_the_limit(box, monkeypatch):
    clock = {"t": 1000.0}
    monkeypatch.setattr(drv, "now", lambda: clock["t"])
    m = drv.Monitor({"test_command": ""}, {"max_wall_clock_s": 100, "wrap_up_frac": 0.8,
                                           "early_stop_check_frac": 2.0})
    clock["t"] = 1079.0
    m.check()
    clock["t"] = 1081.0
    with pytest.raises(drv.Killed, match="wrap_up"):
        m.check()
    m.reason = None
    m.check()  # only once
    clock["t"] = 1101.0
    with pytest.raises(drv.Killed, match="budget_time"):
        m.check()


class Orch:
    """UNIT TEST MOCK: the parts of the orchestrator a hold looks at."""

    def __init__(self, active):
        self.store = type("S", (), {"session_has_active_run": lambda s, sid: active})()


def _episode():
    ep = EvalEpisode.__new__(EvalEpisode)
    ep.session_id = "s"
    ep.holds = []
    ep.waits = []
    ep._wait_background = lambda orch, label="": ep.waits.append(label)
    return ep


def test_the_next_request_waits_for_a_running_redesign_and_records_it():
    ep = _episode()
    ep._quiet = lambda orch: False
    ep._hold_for_run(Orch(True), 2)
    assert len(ep.waits) == 1
    assert ep.holds[0]["after_stage"] == 3 and ep.holds[0]["run_active"] is True


def test_nothing_running_means_no_hold():
    ep = _episode()
    ep._quiet = lambda orch: True
    ep._hold_for_run(Orch(False), 0)
    assert ep.waits == [] and ep.holds == []
