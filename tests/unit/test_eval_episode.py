"""Eval episode tests.

The full pipeline (design review -> gate -> run -> workers ->
selection -> delivery) runs for real over a staged task; only the Sail
surface and the model calls are fakes (UNIT TEST MOCKS). The main agent is
the deterministic ScriptedAgent.
"""

from __future__ import annotations

import json
import sqlite3
import time

import pytest

from hindsight.config import HindsightConfig
from hindsight.delivery import DELIVERY_MARKER
from hindsight.eval.episode import SIMULATED_CONSENT, EvalEpisode
from hindsight.eval.harness import ScriptedAgent
from hindsight.eval.tasks import Task, closing_stage
from tests.unit.test_eval_tasks import (
    COUNTER_NAIVE,
    TODOS_FINAL,
)
from tests.unit.test_orchestrator_flow import FakeSailClient

TASKS_DIR = "eval/tasks"

# ------------------------------------------------------ scripted model fns

REVIEW_YES = json.dumps({
    "latest_request_needs": "x", "what_was_true_before": "y",
    "what_no_longer_holds": "a shared store from the start",
    "call_background": True})


def fake_judge_fn(prompt: str) -> str:
    """UNIT TEST MOCK: critics rank A first; the chairman writes a plan."""
    return "FINAL RANKING:\n1. Proposal A\n2. Proposal B\n\n###DIRECTIVE\nBuild it."


def fake_model_fn(prompt: str) -> str:
    """UNIT TEST MOCK: the design review says yes to every later request."""
    return REVIEW_YES if "call_background" in prompt else "A redesign is ready."


def counter_agent_script():
    shared = (
        "import os\n"
        "\n"
        "\n"
        "class Counter:\n"
        "    def __init__(self):\n"
        "        self._path = os.environ.get(\"COUNTER_STORE\", \"counter_state\")\n"
        "\n"
        "    def _read(self):\n"
        "        try:\n"
        "            with open(self._path) as f:\n"
        "                return int(f.read() or 0)\n"
        "        except FileNotFoundError:\n"
        "            return 0\n"
        "\n"
        "    def increment(self):\n"
        "        v = self._read() + 1\n"
        "        with open(self._path, \"w\") as f:\n"
        "            f.write(str(v))\n"
        "        return v\n"
        "\n"
        "    def get_value(self):\n"
        "        return self._read()\n"
    )
    return [
        {"reply": "built the single-process counter with tests",
         "files": {"counter.py": COUNTER_NAIVE,
                   "test_counter.py": "from counter import Counter\n"
                                      "\n"
                                      "def test_counts():\n"
                                      "    c = Counter()\n"
                                      "    c.increment()\n"
                                      "    assert c.get_value() == 1\n"}},
        {"reply": "moved the counter to a shared store",
         "files": {"counter.py": shared}},
        {"reply": "the file-backed counter already survives restarts",
         "files": {}},
    ]


def todos_agent_script():
    return [
        {"reply": "built the todo module", "files": {"todos.py": TODOS_FINAL}},
        {"reply": "added optional due dates", "files": {}},
        {"reply": "added pending()", "files": {}},
    ]


FAST = {"settle_seconds": 0.05, "quiet_poll_seconds": 0.02,
        "worker_poll_seconds": 0.02}


class ReviewingAgent(ScriptedAgent):
    """A script covers the user's requests. Review turns come on top, whenever
    a design lands, and change no files."""

    def send(self, message: str) -> str:
        if message.startswith(DELIVERY_MARKER) or message == SIMULATED_CONSENT:
            self.messages.append(message)
            return "reviewed"
        return super().send(message)


@pytest.fixture()
def counters_repo(tmp_path):
    task = Task.load(f"{TASKS_DIR}/counters.json")
    repo = task.materialize(tmp_path / "repo")
    return task, repo, tmp_path


def test_treatment_episode_full_pipeline(counters_repo):
    """Stage 2 changes the requirements: a run -> selection -> a nudge is
    delivered. Everything measured honestly."""
    task, repo, tmp_path = counters_repo
    agent = ScriptedAgent(repo, counter_agent_script())
    episode = EvalEpisode(
        task, repo, main_agent=agent, hindsight=True,
        hindsight_home=tmp_path / "hs",
        sail=FakeSailClient(),
        model_fn=fake_model_fn,
        judge_model_fn=fake_judge_fn,
        settings_overrides={**FAST, "quiet_timeout": 60},
        arm="treatment")

    result = episode.run()

    assert result.turns == 3
    assert result.nudge_count >= 1, "the design break must produce a nudge"
    assert result.runs, "a redesign run must have been planned"
    assert all(r["state"] in ("DELIVERED", "READY_TO_DELIVER",
                              "NONE_QUALIFIED", "DROPPED_STALE",
                              "ALL_FAILED") for r in result.runs)
    # The hidden tests ran for real against the scripted agent's code.
    assert result.final.ran
    assert result.final.failed == 0
    # Box time was cost-recorded.
    assert result.spend_usd >= 0.0


def test_treatment_relay_arm_hands_the_design_to_the_agent(counters_repo):
    """What the plugin does: the design is the agent's own turn, the user's
    requests are untouched, and the agent waits for the user's answer."""
    task, repo, tmp_path = counters_repo
    agent = ReviewingAgent(repo, counter_agent_script())
    episode = EvalEpisode(
        task, repo, main_agent=agent, hindsight=True,
        hindsight_home=tmp_path / "hs", sail=FakeSailClient(),
        model_fn=fake_model_fn,
        judge_model_fn=fake_judge_fn,
        settings_overrides={**FAST, "quiet_timeout": 60},
        relay_nudges=True, arm="treatment_relay")

    result = episode.run()

    assert result.nudge_count >= 1
    i = next(n for n, m in enumerate(agent.messages)
             if m.startswith(DELIVERY_MARKER))
    assert agent.messages[i + 1] == SIMULATED_CONSENT
    texts = [n.get("text") or "" for n in result.nudges]
    assert any(t.strip()[:60] in agent.messages[i] for t in texts if t)
    for stage in task.stages:
        assert stage.message in agent.messages


def test_full_context_arm_states_everything_upfront(counters_repo):
    """Arm 2: every request arrives in
    ONE up-front brief — the same information, nothing late. One turn,
    hindsight off, graded identically. This is the information ceiling
    the streamed (baseline) and diff-delivery (treatment_diff) arms
    are read against."""
    task, repo, tmp_path = counters_repo
    # The agent designs once, with complete knowledge, and goes straight
    # to the final shared-store design (the same implementation the
    # streamed agent only reaches in turn 2).
    final_counter = counter_agent_script()[1]["files"]["counter.py"]
    agent = ScriptedAgent(repo, [{
        "reply": "built the complete counter for all requirements",
        "files": {"counter.py": final_counter,
                  "test_counter.py": "from counter import Counter\n"
                                     "\n"
                                     "def test_counts():\n"
                                     "    c = Counter()\n"
                                     "    c.increment()\n"
                                     "    assert c.get_value() == 1\n"}}])
    episode = EvalEpisode(
        task, repo, main_agent=agent, hindsight=False,
        hindsight_home=tmp_path / "hs", sail=FakeSailClient(),
        settings_overrides={**FAST, "quiet_timeout": 60},
        arm="full_context")

    result = episode.run()

    # ONE turn, one message, all three requests inside it.
    assert result.turns == 1
    assert len(agent.messages) == 1
    for stage in task.stages:
        assert stage.message.strip() in agent.messages[0]
    assert "up front" in agent.messages[0]
    # Graded the same as every arm: the final repo vs hidden tests.
    assert result.final.ran
    assert result.final.failed == 0
    assert result.nudge_count == 0


def test_full_context_arm_refuses_hindsight(counters_repo):
    """Loudly refuse full_context + hindsight — the arm measures the
    main agent alone; a silent no-op would poison the comparison."""
    task, repo, tmp_path = counters_repo
    with pytest.raises(ValueError, match="full_context"):
        EvalEpisode(
            task, repo, main_agent=ScriptedAgent(repo, []),
            hindsight=True, hindsight_home=tmp_path / "hs",
            sail=FakeSailClient(), arm="full_context")


def test_treatment_diff_arm_delivers_patch_to_agent(counters_repo):
    """The full diff goes to the main agent, with its basis, and the agent may
    not apply it before the user agrees."""
    task, repo, tmp_path = counters_repo
    agent = ReviewingAgent(repo, counter_agent_script())
    episode = EvalEpisode(
        task, repo, main_agent=agent, hindsight=True,
        hindsight_home=tmp_path / "hs", sail=FakeSailClient(),
        model_fn=fake_model_fn,
        judge_model_fn=fake_judge_fn,
        settings_overrides={**FAST, "quiet_timeout": 60},
        relay_nudges=True, relay_diff=True, arm="treatment_diff")

    result = episode.run()

    assert result.nudge_count >= 1
    msg = next(m for m in agent.messages if m.startswith(DELIVERY_MARKER))
    assert "THE DIFF" in msg
    assert "diff --git a/counter.py b/counter.py" in msg
    assert "+new = 2" in msg
    assert "conversation message #" in msg
    assert "until the user agrees" in msg
    for stage in task.stages:
        assert stage.message in agent.messages


class NothingToChangeSail(FakeSailClient):
    """UNIT TEST MOCK: every worker reports the code already does what was asked."""

    def read_file(self, box_id, path):
        if path.endswith("patch.diff"):
            return b""
        return super().read_file(box_id, path)


def test_no_change_task_produces_zero_nudges(tmp_path):
    """The control: when the code already does what was asked, a new request
    still starts a run (it has to look), but nothing is ever sent. The right
    number of nudges here is zero."""
    task = Task.load(f"{TASKS_DIR}/todos.json")
    repo = task.materialize(tmp_path / "repo")
    agent = ScriptedAgent(repo, todos_agent_script())
    episode = EvalEpisode(
        task, repo, main_agent=agent, hindsight=True,
        hindsight_home=tmp_path / "hs",
        sail=NothingToChangeSail(),
        model_fn=fake_model_fn,
        judge_model_fn=fake_judge_fn,
        settings_overrides={**FAST, "quiet_timeout": 60},
        arm="treatment")

    result = episode.run()

    assert result.nudge_count == 0
    assert result.runs
    assert all(r["state"] == "NONE_QUALIFIED" for r in result.runs)
    assert result.final.failed == 0
    assert result.final.passed > 0


def test_baseline_alone_runs_without_hindsight(counters_repo):
    """Baseline A: the main agent alone — no orchestrator, no records
    beyond the scored hidden tests."""
    task, repo, tmp_path = counters_repo
    agent = ScriptedAgent(repo, counter_agent_script())
    episode = EvalEpisode(task, repo, main_agent=agent, hindsight=False,
                          arm="baseline_alone")

    result = episode.run()

    assert result.turns == 3
    assert result.nudges == []
    assert result.runs == []
    assert result.final.failed == 0


def test_episode_config_overrides_apply(counters_repo):
    task, repo, tmp_path = counters_repo
    agent = ScriptedAgent(repo, counter_agent_script())
    episode = EvalEpisode(
        task, repo, main_agent=agent, hindsight=True,
        hindsight_home=tmp_path / "hs", sail=FakeSailClient(),
        model_fn=fake_model_fn,
        judge_model_fn=fake_judge_fn,
        settings_overrides={**FAST, "report_bugs": True, "quiet_timeout": 60},
        arm="treatment")
    orch = episode._build_orchestrator()
    try:
        cfg = orch.cfg
        assert cfg.experiments.report_bugs is True
        assert cfg.experiments.settle_seconds == FAST["settle_seconds"]
        assert cfg.project.test_command == task.test_command
    finally:
        orch.shutdown()


def test_episode_models_override_applies_and_rejects_unknown(counters_repo):
    """Weak-model experiments: models_override must reach
    ModelsConfig, and a typo'd field must fail loudly (never silently
    no-op — that would fake a model swap)."""
    task, repo, tmp_path = counters_repo
    agent = ScriptedAgent(repo, counter_agent_script())
    episode = EvalEpisode(
        task, repo, main_agent=agent, hindsight=True,
        hindsight_home=tmp_path / "hs", sail=FakeSailClient(),
        model_fn=fake_model_fn,
        judge_model_fn=fake_judge_fn,
        settings_overrides={**FAST, "quiet_timeout": 60},
        models_override={"worker": "zai-org/GLM-5.3-Flash",
                         "reviewer": "zai-org/GLM-5.3-Flash"},
        arm="treatment")
    orch = episode._build_orchestrator()
    try:
        assert orch.cfg.models.worker == "zai-org/GLM-5.3-Flash"
        assert orch.cfg.models.reviewer == "zai-org/GLM-5.3-Flash"
        # Untouched fields keep config defaults (judge stays cross-family).
        assert orch.cfg.models.judge == "moonshotai/Kimi-K3"
    finally:
        orch.shutdown()

    bad = EvalEpisode(
        task, repo, main_agent=agent, hindsight=True,
        hindsight_home=tmp_path / "hs2", sail=FakeSailClient(),
        settings_overrides={**FAST, "quiet_timeout": 60},
        models_override={"workr": "zai-org/GLM-5.3-Flash"},
        arm="treatment")
    with pytest.raises(ValueError, match="workr"):
        bad._build_orchestrator()


def test_episode_records_turn_files(counters_repo):
    """Regression (found live, probe4): the episode emitted the
    user_message event AFTER main_agent.send() returned, so every turn's
    pre and post snapshots were identical — turn_files always empty, the
    ledger's changed_files always "(none)", and therefore no live episode
    could ever record design assumptions or fire the gate. The events must
    arrive in production order: user_message BEFORE the agent works."""
    task, repo, tmp_path = counters_repo

    class WritingAgent:
        def __init__(self):
            self.n = 0

        def send(self, message):
            self.n += 1
            (repo / "counter.py").write_text(
                f"class Counter:\n    n = {self.n}\n")
            return f"done {self.n}"

        def close(self):
            pass

    episode = EvalEpisode(
        task, repo, main_agent=WritingAgent(), hindsight=True,
        hindsight_home=tmp_path / "hs", sail=FakeSailClient(),
        model_fn=lambda p: json.dumps({"ops": []}),
        settings_overrides={**FAST, "quiet_timeout": 60}, arm="treatment")
    result = episode.run(stages=task.stages[:2])
    assert result.turns == 2

    import sqlite3
    c = sqlite3.connect(tmp_path / "hs" / "events.sqlite3")
    c.row_factory = sqlite3.Row
    try:
        files_per_turn = [
            [r["path"] for r in c.execute(
                "SELECT path FROM turn_files WHERE turn_id=?", (t["id"],))]
            for t in c.execute("SELECT id FROM turns ORDER BY n")]
        assert files_per_turn == [["counter.py"], ["counter.py"]], \
            "agent file writes between the two events must be recorded"
    finally:
        c.close()


# ------------------------------------------- dropped final-turn chain (probe4/6)

def slow_turn3_model_fn(delay: float):
    """A design review that takes `delay` seconds on turn 3 and says yes only
    then, so any nudge must come from the final turn: the path that used to be
    dropped at shutdown (two trials)."""
    def fn(prompt: str) -> str:
        if "call_background" not in prompt:
            return "A redesign is ready."
        latest = prompt[prompt.index("LATEST REQUEST:"):prompt.index("THE CODE:")]
        if "survive service restarts" in latest:
            time.sleep(delay)
            return REVIEW_YES
        return json.dumps({"call_background": False})
    return fn


def test_final_turn_chain_survives_slow_review(counters_repo):
    """Regression (two trials): the episode must wait for the last turn's
    settle verdict AND gate decision before scoring, however slow the
    turn's review is. Unfixed, the stage-3 conflict below is
    dropped at shutdown: usable stays NULL, no third session check, and
    no nudge — the final-turn nudge path is the one the eval exists to
    measure."""
    task, repo, tmp_path = counters_repo
    agent = ScriptedAgent(repo, counter_agent_script())
    episode = EvalEpisode(
        task, repo, main_agent=agent, hindsight=True,
        hindsight_home=tmp_path / "hs",
        sail=FakeSailClient(),
        model_fn=slow_turn3_model_fn(1.5),
        judge_model_fn=fake_judge_fn,
        settings_overrides={**FAST, "flush_timeout": 0.3,
                            "quiet_timeout": 120},
        arm="treatment")

    result = episode.run()

    # Every turn's settle chain completed — including the last one.
    import sqlite3
    c = sqlite3.connect(tmp_path / "hs" / "events.sqlite3")
    c.row_factory = sqlite3.Row
    try:
        turns = list(c.execute("SELECT id, usable FROM turns ORDER BY n"))
        assert len(turns) == 3
        assert all(t["usable"] is not None for t in turns), \
            "the final turn's settle verdict was never recorded"
        checks = list(c.execute(
            "SELECT turn_id FROM session_checks ORDER BY ts"))
        assert len(checks) == 3, "one gate decision per turn, incl. the last"
        assert checks[-1]["turn_id"] == turns[-1]["id"]
    finally:
        c.close()
    # The stage-3 conflict (the only one) fired its gate and produced a
    # nudge — the previously-dead final-turn path.
    assert result.nudge_count >= 1, \
        "a stage-3 design break must be able to nudge before scoring"
    assert result.runs, "the stage-3 conflict must launch a redesign run"


def test_quiet_requires_settle_and_gate_of_latest_turn(counters_repo):
    """Unit view of the same fix: _quiet must be False while the latest
    turn's settle verdict or gate decision is still outstanding."""
    from hindsight.orchestrator import Orchestrator

    task, repo, tmp_path = counters_repo
    orch = Orchestrator(
        HindsightConfig(project_dir=repo, hindsight_home=tmp_path / "hs"),
        model_fn=lambda p: json.dumps({"ops": []}),
        sail=FakeSailClient())
    episode = EvalEpisode(
        task, repo, main_agent=ScriptedAgent(repo, []), hindsight=True,
        hindsight_home=tmp_path / "hs", session_id="s-quiet",
        arm="treatment")
    try:
        orch.store.upsert_session("s-quiet", "p", "opencode", "unknown")
        turn_id = orch.store.start_turn("s-quiet", 1, "m1", None)
        orch.store.complete_turn(turn_id, "ok", None)
        assert episode._quiet(orch) is False, "usable=NULL is not quiet"
        orch.store.mark_turn_usable(turn_id, True, "settled")
        assert episode._quiet(orch) is False, \
            "no gate decision for the latest turn is not quiet"
        orch.store.save_session_check(
            "s-quiet", turn_id, False, "low", {}, [])
        assert episode._quiet(orch) is True
    finally:
        orch.shutdown()


def test_wait_background_honors_undrained_event_queue(counters_repo):
    """An undrained event queue is not quiet, whatever the recorded turns
    look like (2026-11-01: the fork episode's final turn was never
    recorded because its handler queued behind a stuck callback, and
    the quiet check passed anyway)."""
    from hindsight.orchestrator import Orchestrator

    task, repo, tmp_path = counters_repo
    orch = Orchestrator(
        HindsightConfig(project_dir=repo, hindsight_home=tmp_path / "hs"),
        model_fn=lambda p: json.dumps({"ops": []}),
        sail=FakeSailClient())
    episode = EvalEpisode(
        task, repo, main_agent=ScriptedAgent(repo, []), hindsight=True,
        hindsight_home=tmp_path / "hs", arm="treatment",
        settings_overrides={**FAST, "flush_timeout": 0.2,
                            "quiet_timeout": 2.0})
    real_flush = orch.flush

    def never_drained(timeout: float = 60.0) -> bool:
        return False  # a handler stuck behind a long callback

    try:
        orch.flush = never_drained
        t0 = time.monotonic()
        episode._wait_background(orch, label="test")
        waited = time.monotonic() - t0
        # It waits out the whole quiet timeout: it must NOT return early just
        # because no work is visible.
        assert waited >= 2.0, \
            f"returned after {waited:.1f}s despite an undrained queue"
        # Sanity: with a drained queue the same call returns promptly.
        orch.flush = real_flush
        t0 = time.monotonic()
        episode._wait_background(orch, label="test2")
        assert time.monotonic() - t0 < 5.0
    finally:
        orch.shutdown()


def test_design_from_the_last_stage_lands_and_is_not_observed(counters_repo):
    """A design built after the last stage still reaches the agent, and neither
    its review nor the closing turn counts as a turn of the session."""
    task, repo, tmp_path = counters_repo
    short = task.model_copy(update={"stages": task.stages[:2]})
    script = counter_agent_script()[:2] + [{"reply": "all tidy"}]
    agent = ReviewingAgent(repo, script)
    episode = EvalEpisode(
        short, repo, main_agent=agent, hindsight=True,
        hindsight_home=tmp_path / "hs", sail=FakeSailClient(),
        model_fn=fake_model_fn,
        judge_model_fn=fake_judge_fn,
        settings_overrides={**FAST, "quiet_timeout": 60},
        relay_nudges=True, arm="treatment_relay",
        extra_stages=[closing_stage()])

    result = episode.run()

    assert result.nudge_count >= 1
    assert any(m.startswith(DELIVERY_MARKER) for m in agent.messages)
    assert closing_stage().message in agent.messages
    con = sqlite3.connect(tmp_path / "hs" / "events.sqlite3")
    assert con.execute("SELECT COUNT(*) FROM turns").fetchone()[0] == 2


def test_baseline_gets_the_same_closing_turn(counters_repo):
    task, repo, tmp_path = counters_repo
    agent = ScriptedAgent(repo, counter_agent_script() + [{"reply": "tidy"}])
    result = EvalEpisode(
        task, repo, main_agent=agent, hindsight=False,
        hindsight_home=tmp_path / "hs", arm="baseline_alone",
        extra_stages=[closing_stage()]).run()
    assert result.turns == 4
    assert agent.messages[-1] == closing_stage().message


def test_full_context_gets_closing_as_its_own_turn(counters_repo):
    task, repo, tmp_path = counters_repo
    final_counter = counter_agent_script()[1]["files"]["counter.py"]
    agent = ScriptedAgent(repo, [
        {"reply": "built", "files": {"counter.py": final_counter}},
        {"reply": "tidy"}])
    result = EvalEpisode(
        task, repo, main_agent=agent, hindsight=False,
        hindsight_home=tmp_path / "hs", arm="full_context",
        extra_stages=[closing_stage()]).run()
    assert result.turns == 2
    assert closing_stage().message not in agent.messages[0]
    assert agent.messages[1] == closing_stage().message


