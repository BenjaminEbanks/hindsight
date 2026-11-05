"""The EvoCode runner's agent turns, with a fake box. No model or box is used."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest
from types import SimpleNamespace

from hindsight.eval.episode import SIMULATED_CONSENT
from hindsight.sail_client import ExecOut

_spec = importlib.util.spec_from_file_location(
    "evocode_pilot", Path(__file__).parents[2] / "scripts" / "evocode_pilot.py")
ep = importlib.util.module_from_spec(_spec)
sys.modules["evocode_pilot"] = ep
_spec.loader.exec_module(ep)

EVENTS = json.dumps({"type": "step_finish", "sessionID": "ses-1", "part": {
    "type": "step-finish",
    "tokens": {"input": 10, "output": 5, "reasoning": 0, "cache": {"read": 90, "write": 0}}}})
TEXT = json.dumps({"type": "text", "part": {"type": "text", "text": "done"}})


class FakeBox:
    def __init__(self, out=EVENTS + "\n" + TEXT):
        self.out = out
        self.commands: list[str] = []
        self.files: dict = {}

    def run(self, box_id, command, *, timeout=None, cwd=None, env=None):
        cmd = command[-1] if isinstance(command, list) else command
        self.commands.append(cmd)
        if "out.jsonl" in cmd and cmd.startswith("cat"):
            return ExecOut(0, self.out, "", False)
        if "reward_details" in cmd:
            return ExecOut(0, "", "", False)
        if "reward.txt" in cmd:
            return ExecOut(0, "1.0\n", "", False)
        if "verify.log" in cmd:
            return ExecOut(0, "CASE_SUMMARY total_cases=4 success_count=4 fail_count=0\n", "", False)
        if ".exit" in cmd:
            return ExecOut(0, "0\n", "", False)
        return ExecOut(0, "", "", False)

    def start_background(self, box_id, command, *, cwd=None, env=None):
        self.commands.append(command)

    def write_files(self, box_id, files):
        self.files.update(files)

    def upload_file(self, box_id, local, guest):
        pass

    def download_file(self, box_id, guest, local):
        import tarfile
        with tarfile.open(local, "w:gz"):
            pass


@pytest.fixture()
def agent(tmp_path, monkeypatch):
    monkeypatch.setattr(ep, "POLL_SECONDS", 0)
    task = tmp_path / "task"
    (task / "steps" / "round-1" / "tests").mkdir(parents=True)
    (task / "steps" / "round-1" / "tests" / "test.sh").write_text("true")
    (task / "steps" / "round-1" / "instruction.md").write_text("build it")
    repo = tmp_path / "repo"
    repo.mkdir()
    (tmp_path / "raw").mkdir()
    cfg = {"agent": {"timeout_sec": 10.0}, "verifier": {"timeout_sec": 10.0},
           "metadata": {"requirement_chain": {"steps": [
               {"step": "round-1", "change_types": ["conflict"]}]}}}
    result = {"task": "t", "model": "x/GLM-5.3-Flash", "rounds": [], "hindsight_turns": [],
              "error": ""}

    def make(box=None):
        return ep.BoxAgent(box or FakeBox(), "b", task, cfg, [1], "ses-1", "x/GLM-5.3-Flash",
                           tmp_path / "raw", repo, result, tmp_path, ep.Spend(10.0)), result

    return make


def test_round_message_is_graded_as_a_round(agent):
    a, result = agent()
    assert a.send("build it") == "done"
    row = result["rounds"][0]
    assert row["round"] == 1 and row["passed"] is True
    assert row["cases_passed"] == 4 and row["change_types"] == ["conflict"]
    assert row["tokens"]["cache_read"] == 90


def test_delivery_is_recorded_apart_and_consent_regrades_the_round(agent):
    a, result = agent()
    a.send("build it")
    a.send("[Hindsight delivery] a design")
    a.send(SIMULATED_CONSENT)
    assert len(result["rounds"]) == 1
    delivery, consent = result["hindsight_turns"]
    assert delivery["kind"] == "delivery" and "grade_after" not in delivery
    assert consent["kind"] == "consent" and consent["grade_after"]["passed"] is True
    assert consent["after_round"] == 1


def test_tests_are_gone_before_the_agent_works_again(agent):
    box = FakeBox()
    a, _ = agent(box)
    a.send("build it")
    removed = next(i for i, c in enumerate(box.commands) if "rm -rf /tests" in c)
    assert removed > next(i for i, c in enumerate(box.commands) if "test.sh" in c)


def test_an_agent_that_never_ran_stops_the_task(agent):
    a, result = agent(FakeBox(out=""))
    with pytest.raises(RuntimeError, match="never ran"):
        a.send("build it")
    assert result["rounds"] == []


def test_spend_limit_stops_before_another_turn(agent):
    a, _ = agent()
    a.spend = ep.Spend(0.0)
    with pytest.raises(RuntimeError, match="spend limit"):
        a.send("build it")


def test_no_key_reaches_the_box_config(tmp_path, monkeypatch):
    home = tmp_path
    (home / ".config/opencode").mkdir(parents=True)
    (home / ".config/opencode/opencode.json").write_text(json.dumps(
        {"provider": {"p": {"options": {"apiKey": "real-secret", "baseURL": "u"}}}}))
    monkeypatch.setattr(Path, "home", lambda: home)
    cfg = ep.opencode_config()
    assert "real-secret" not in cfg and "{env:SAIL_API_KEY}" in cfg


def test_forced_review_says_yes_once_and_marks_the_result():
    result = {}
    fn = ep.forced_review(3, result)
    verdicts = [json.loads(fn("p"))["call_background"] for _ in range(4)]  # rounds 2-5
    assert verdicts == [False, True, False, False]
    assert result["forced_review_turn"] == 3


def test_no_test_results_stay_in_the_box(agent):
    box = FakeBox()
    a, _ = agent(box)
    a.send("build it")
    cleanup = [c for c in box.commands if "rm -rf /tests" in c]
    assert cleanup and "/work/verify.log" in cleanup[-1] and "/logs/verifier" in cleanup[-1]
    assert any("rm -f /work/mirror.tar.gz" in c for c in box.commands)


def test_a_long_design_reaches_the_agent_as_text(tmp_path):
    from types import SimpleNamespace

    from hindsight.eval.episode import EvalEpisode
    from hindsight.store import Store

    store = Store(tmp_path / "e.sqlite3")
    long_patch = "diff --git a/x b/x\n" + "+line\n" * 10_000
    ref = store._blob_set(long_patch)
    assert ref.startswith("file:")
    sent = []
    ep_ = EvalEpisode.__new__(EvalEpisode)
    ep_.relay_nudges, ep_.relay_diff, ep_.max_diff_kwargs = True, True, {"max_diff_chars": 90_000}
    ep_.orch = SimpleNamespace(store=store)
    ep_.session_id = "s1"
    shared = {}
    ep_.main_agent = SimpleNamespace(
        send=sent.append,
        share_file=lambda name, text: shared.update({name: text}) or f"/work/hindsight/{name}")
    ep_._relay_queue = [{"text": "a design", "patch_blob": ref, "basis_turn_n": 1,
                         "missed_json": "[]"}]
    ep_._hand_over_designs()
    assert ref not in sent[0] and "+line" in sent[0] and sent[1] == SIMULATED_CONSENT
    # 60 KB fits under the limit, so nothing needs a file.
    assert not shared
    ep_.max_diff_kwargs = {"max_diff_chars": 100, "has_tools": False}
    ep_._relay_queue = [{"id": "n1", "text": "a design", "patch_blob": ref, "basis_turn_n": 1,
                         "missed_json": "[]"}]
    ep_._hand_over_designs()
    assert shared["design-n1.diff"] == long_patch and "/work/hindsight/design-n1.diff" in sent[2]
    store.close()


def test_the_agent_gets_a_placeholder_not_the_key(agent, monkeypatch):
    monkeypatch.setenv("SAIL_API_KEY", "real-secret")
    box = FakeBox()
    envs = []
    start = box.start_background

    def recording(box_id, command, *, cwd=None, env=None):
        envs.append(env)
        start(box_id, command, cwd=cwd, env=env)

    box.start_background = recording
    a, _ = agent(box)
    a.send("build it")
    assert envs and "real-secret" not in str(envs)
    assert {"SAIL_API_KEY": ep.BOX_KEY_PLACEHOLDER} in envs


def test_gemma_cached_input_uses_its_own_price():
    tokens = {"input": 1_000_000, "output": 1_000_000, "reasoning": 0,
              "cache_read": 1_000_000, "cache_write": 0}
    assert ep._usd(tokens, ep.usd_rates("sail-research/nvidia/Gemma-4-31B-IT-NVFP4")) == pytest.approx(0.49)


def test_a_box_never_runs_without_its_requested_policy(monkeypatch):
    import hindsight.sail_client as sc

    calls = {"terminated": 0}

    class Box:
        sailbox_id = "b1"

        def set_egress_policy(self, policy):
            raise RuntimeError("refused")

        def terminate(self):
            calls["terminated"] += 1

    saved = SimpleNamespace(name="p")
    monkeypatch.setattr(sc.EgressPolicy, "list", staticmethod(lambda: [saved]))
    monkeypatch.setattr(sc.Sailbox, "from_checkpoint", staticmethod(lambda cid, name: Box()))
    client = sc.SailClient(box_policy="p")
    with pytest.raises(sc.SailClientError):
        client.create_box("x", from_checkpoint_id="c")
    assert calls["terminated"] == 1


def test_a_missing_policy_stops_box_creation(monkeypatch):
    import hindsight.sail_client as sc

    monkeypatch.setattr(sc.EgressPolicy, "list", staticmethod(lambda: []))
    made = []
    monkeypatch.setattr(sc.Sailbox, "from_checkpoint", staticmethod(lambda *a, **k: made.append(1)))
    with pytest.raises(sc.SailClientError, match="no saved egress policy"):
        sc.SailClient(box_policy="p").create_box("x", from_checkpoint_id="c")
    assert not made


def test_a_delivery_without_tools_does_not_mention_them():
    from hindsight.delivery import render_agent_message

    msg = render_agent_message("facts", "x" * 50, 2, max_diff_chars=10, has_tools=False)
    assert "hindsight_" not in msg and "no Hindsight tools" in msg
    assert "hindsight_adopt" in render_agent_message("facts", "x", 2)


def test_slopcode_scores_are_kept_and_a_round_needs_every_test():
    details = {"core_collected": 2, "core_passed": 2, "regression_collected": 2, "regression_passed": 1,
               "strict_pass_rate": 0.75, "isolated_pass_rate": 1.0, "core_pass_rate": 1.0,
               "verbosity": 0.2, "erosion": 0.4}
    got = ep.slopcode_grade(details)
    assert not got["passed"]
    assert (got["cases_passed"], got["cases_total"]) == (3, 4)
    assert got["slopcode"]["erosion"] == 0.4
    assert ep.slopcode_grade({"strict_pass_rate": 1.0})["passed"]


def test_slopcode_task_is_laid_out_for_the_runner(tmp_path):
    from importlib import util

    spec = util.spec_from_file_location("sp", Path("scripts/slopcode_prepare.py"))
    sp = util.module_from_spec(spec)
    spec.loader.exec_module(sp)
    src = tmp_path / "file_backup"
    (src / "environment").mkdir(parents=True)
    (src / "environment" / "Dockerfile").write_text("FROM x")
    (src / "tests").mkdir()
    (src / "tests" / "test_checkpoint_1.py").write_text("shared")
    for step in ("checkpoint_1", "checkpoint_2"):
        (src / "steps" / step / "tests").mkdir(parents=True)
        (src / "steps" / step / "solution").mkdir()
        (src / "steps" / step / "tests" / "test.sh").write_text(step)
        (src / "steps" / step / "instruction.md").write_text(f"do {step}")
    limits = "[steps.agent]\ntimeout_sec = 7.0\n[steps.verifier]\ntimeout_sec = 9.0\n"
    (src / "task.toml").write_text(
        '[metadata]\nsource_problem = "file_backup"\n'
        f'[[steps]]\nname = "checkpoint_1"\n{limits}[[steps]]\nname = "checkpoint_2"\n{limits}')
    dest = sp.prepare(src, tmp_path / "out")
    assert ep.round_numbers(dest) == [1, 2]
    cfg = ep.task_config(dest)
    assert cfg["metadata"]["name"] == "file_backup" and cfg["agent"]["timeout_sec"] == 7.0
    first = (dest / "steps/round-1/instruction.md").read_text()
    later = (dest / "steps/round-2/instruction.md").read_text()
    assert "Use a virtual environment" in first and "do checkpoint_1" in first
    assert "Keep using the same virtual environment" in later and "do checkpoint_2" in later
    tests = dest / "steps/round-2/tests"
    assert (tests / "test.sh").read_text() == "checkpoint_2"
    assert (tests / "test_checkpoint_1.py").read_text() == "shared"


def test_a_link_out_of_the_tree_does_not_stop_the_mirror(tmp_path):
    import io
    import tarfile

    from scripts.evocode_pilot import _keep_safe

    path = tmp_path / "a.tar"
    with tarfile.open(path, "w") as tar:
        link = tarfile.TarInfo("env/bin/python3")
        link.type, link.linkname = tarfile.SYMTYPE, "/usr/bin/python3"
        tar.addfile(link)
        info = tarfile.TarInfo("app.py")
        info.size = 2
        tar.addfile(info, io.BytesIO(b"ok"))
    out = tmp_path / "out"
    out.mkdir()
    with tarfile.open(path) as tar:
        tar.extractall(out, filter=_keep_safe)
    assert (out / "app.py").read_text() == "ok"
    assert not (out / "env/bin/python3").exists()


def test_product_quality_leaves_tests_out_and_never_invents_a_score():
    class Box:
        stdout = '{"verbosity": 0.28, "erosion": 0.73, "total_loc": 708}'

    calls = []
    real = ep.sh
    ep.sh = lambda client, box, cmd, timeout=0: (calls.append(cmd), Box)[1]
    try:
        got = ep.product_quality(None, "b")
        assert got == {"verbosity_product": 0.28, "erosion_product": 0.73, "loc_product": 708}
        for name in ("--exclude='tests'", "--exclude='test_*'", "--exclude='*_test.*'",
                     "--exclude='.venv'", "--exclude='venv'", "--exclude='conftest.py'"):
            assert name in calls[0]
        Box.stdout = ""
        assert ep.product_quality(None, "b")["erosion_product"] is None
    finally:
        ep.sh = real
