"""Selection tests. All Sail/model/executor dependencies are injected fakes
(UNIT TEST MOCKS). The honesty rules (a regression disqualifies, an empty test
result is no evidence) are deterministic code under test.
"""

from __future__ import annotations

import pytest

from hindsight.delivery import DeliveryManager

from hindsight.budget import BudgetManager
from hindsight.config import DEFAULT_SNAPSHOT_EXCLUDES, HindsightConfig
from hindsight.env import EnvManager
from hindsight.selection import Selector, _regressions
from hindsight.sail_client import BoxHandle, ExecOut
from hindsight.snapshot import Snapshotter
from hindsight.store import Store
from hindsight.store import built_on_key


def junit(outcomes: dict[str, str]) -> bytes:
    parts = ['<?xml version="1.0"?><testsuite>']
    for name, o in outcomes.items():
        parts.append(f'<testcase name="{name}">')
        if o == "fail":
            parts.append('<failure message="assert" type="AssertionError">assert</failure>')
        elif o == "error":
            parts.append('<error message="boom" type="ImportError">boom</error>')
        elif o == "skip":
            parts.append("<skipped/>")
        parts.append("</testcase>")
    parts.append("</testsuite>")
    return "".join(parts).encode()


class SelectionSail:
    """UNIT TEST MOCK: simulates selection boxes. Box behavior is keyed by the
    box NAME prefix the Selector assigns:
      hs-cur-*     the CURRENT code side
      hs-matrix-*  the PROPOSAL side (patch applied)
      hs-rebase-*  only the rebase (no matrix)
    `apply_fails` makes every `git apply` exit non-zero (conflict).
    """

    def __init__(self, *, apply_fails: bool = False,
                 current: dict[str, str] | None = None,
                 proposal: dict[str, str] | None = None):
        self.apply_fails = apply_fails
        self.current = current if current is not None else {"test_app": "pass"}
        self.proposal = proposal if proposal is not None else {"test_app": "pass"}
        self.boxes: dict[str, dict] = {}
        self.written: dict[str, str] = {}
        self.terminated: list[str] = []
        self.woken: list[str] = []
        self.wake_fails = False
        self.n = 0
        self.apply_calls = 0

    def create_box(self, name, *, image=None, from_checkpoint_id=None,
                   max_lifetime_seconds=None, size=None):
        self.n += 1
        bid = f"box-{self.n}"
        self.boxes[bid] = {"name": name}
        return BoxHandle(id=bid, name=name)

    def run(self, box_id, command, *, timeout=None, cwd=None, env=None):
        cmd = " ".join(command)
        if "git apply" in cmd:
            self.apply_calls += 1
            if self.apply_fails:
                return ExecOut(1, "", "error: patch failed", False)
        return ExecOut(0, "", "", False)

    def upload_file(self, box_id, local_path, guest_path):
        pass

    def write_files(self, box_id, files):
        self.written.update(files)

    def read_file(self, box_id, path):
        if path == "/work/proj_junit.xml":
            name = self.boxes[box_id]["name"]
            return junit(self.current if name.startswith("hs-cur")
                         else self.proposal)
        return b""

    def terminate(self, box_id):
        self.terminated.append(box_id)

    def checkpoint(self, box_id, ttl_seconds=None, name=None):
        return "cp-sel"

    def wake(self, box_id):
        if self.wake_fails:
            from hindsight.sail_client import SailClientError
            raise SailClientError("box gone")
        self.woken.append(box_id)


class FakeExecutor:
    def __init__(self):
        self.submitted = []
        self.extra_prompts = []
        self.followups = []
        self.store = None

    def submit(self, plan, session_id, base_commit, harness="pi", *,
               samples=None, run_id=None, extra_prompt=None, mark_synthesis=False):
        self.submitted.append((plan, samples, run_id))
        self.extra_prompts.append(extra_prompt)
        return [f"ex-esc-{len(self.submitted)}"]

    def submit_followup(self, old, messages):
        ex_id = self.store.create_exploration(
            old["session_id"], old["strategy"], old["harness"],
            old["base_turn_id"], 1, {"synthesis": True}, run_id=old["run_id"])
        self.store.set_box_id(ex_id, old["box_id"])
        self.store.kv_set(built_on_key(ex_id), self.store.kv_get(built_on_key(old["id"])) or "")
        return ex_id

    def start_followup(self, ex_id, messages):
        self.followups.append((ex_id, messages))


class FakeEnv:
    """UNIT TEST MOCK: env with a fixed flaky list and no box work."""

    def __init__(self, flaky=None):
        self.flaky_tests = flaky or []

    def ensure_env(self, commit):
        from hindsight.env import EnvRecord
        return EnvRecord("k", "cp-sel", 1e18, {}, list(self.flaky_tests))


@pytest.fixture()
def parts(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "app.py").write_text("x = 1\n")
    (repo / "test_app.py").write_text("def test_app():\n    assert True\n")
    cfg = HindsightConfig(project_dir=repo, hindsight_home=tmp_path / "hs")
    cfg.experiments.workers_max = 3
    store = Store(cfg.db_path)
    snap = Snapshotter(repo, cfg.snapshot_repo, DEFAULT_SNAPSHOT_EXCLUDES, 5.0)
    snap.ensure()
    commit = snap.snapshot("s1", 1, "post").commit
    # A usable snapshot row so _base_commit resolves.
    turn_id = store.start_turn("s1", 1, "m1", commit)
    store.complete_turn(turn_id, "ok", commit, snapshot_files=1)
    store.mark_turn_usable(turn_id, 1, "test fixture")
    yield cfg, store, snap, commit
    store.close()


def make_selector(cfg, store, snap, sail, *, executor=None,
                  env=None) -> Selector:
    executor = executor or FakeExecutor()
    executor.store = store
    return Selector(
        cfg, store, sail, env or FakeEnv(), snap, executor,
        BudgetManager(store, cfg))


PRODUCT_PATCH = "--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-x = 1\n+x = 2\n"


def add_gating_worker(store, run_id, *, patch=PRODUCT_PATCH, summary="p",
                      diff_stat=None, budget=None, box_id=None) -> str:
    """A finished worker resting in GATING, with its proposal. Returns the
    proposal id."""
    ex_id = store.create_exploration(
        "s1", "clean_rewrite", "opencode", "t1", 1,
        budget or {"target_ids": ["R1"]}, run_id=run_id)
    latest = store.latest_usable_snapshot("s1")
    if latest is not None:
        store.kv_set(built_on_key(ex_id), latest["post_snapshot"])
    for state in ("PROVISIONING", "RUNNING", "TESTING", "GATING"):
        store.transition_exploration(ex_id, state, "seed")
    if box_id:
        store.set_box_id(ex_id, box_id)
    return store.save_proposal(
        ex_id, patch, summary if isinstance(summary, dict) else {"summary": summary},
        diff_stat or {"files": 1, "added": 10, "removed": 2})


def seed_run(store, *, n_proposals=1, diff_stats=None, final=False) -> str:
    """A LAUNCHED run with n GATING explorations, each with a proposal. `final`
    makes them final-worker designs, the only ones that get checked."""
    run_id = store.create_run("s1", 1, ["R1"])
    store.transition_run(run_id, "PLANNED", "seed")
    for i in range(n_proposals):
        add_gating_worker(
            store, run_id, summary=f"proposal {i}",
            diff_stat=(diff_stats or [None] * n_proposals)[i],
            budget={"target_ids": ["R1"], "synthesis": True} if final else None)
    store.transition_run(run_id, "LAUNCHED", "seed")
    return run_id


def test_final_design_that_passes_is_ready_to_send(parts):
    cfg, store, snap, commit = parts
    sail = SelectionSail()
    run_id = seed_run(store, final=True)
    sel = make_selector(cfg, store, snap, sail)

    sel.on_exploration_finished(run_id)

    run = store.get_run(run_id)
    assert run["state"] == "READY_TO_DELIVER"
    stages = {r["stage"] for r in store._query(
        "SELECT stage FROM gate_results WHERE proposal_id=?",
        (run["winner_proposal_id"],))}
    assert "1_rebase" not in stages and "2_audit_gate" in stages
    p = store.get_proposal(run["winner_proposal_id"])
    assert p["qualified"] is True and p["regressions"] == []
    # Every selection box was cleaned up.
    assert len(sail.terminated) == len(sail.boxes)


def test_regression_after_the_retry_is_sent_with_the_failures(parts):
    cfg, store, snap, commit = parts
    sail = SelectionSail(proposal={"test_app": "fail"})
    run_id = seed_run(store, final=True)
    store.kv_set(f"final_retried:{run_id}", "1")
    sel = make_selector(cfg, store, snap, sail)

    sel.on_exploration_finished(run_id)

    assert store.get_run(run_id)["state"] == "READY_TO_DELIVER"
    p = store.run_proposals(run_id)[0]
    assert p["qualified"] is False
    assert p["regressions"] == ["test_app"]
    assert store.exploration_state(p["exploration_id"]) == "NUDGE_READY"

    dm = DeliveryManager(cfg, store, lambda _: "An alternative is available.")
    dm.maybe_deliver("s1")
    text = store.pending_agent_deliveries("s1")[0]["text"]
    assert "CHECK THESE" in text and "test_app" in text
    assert "with no regressions" not in text


def test_a_design_whose_tests_never_ran_sends_nothing(parts):
    cfg, store, snap, commit = parts
    sail = SelectionSail(proposal={})
    run_id = seed_run(store, final=True)
    store.kv_set(f"final_retried:{run_id}", "1")
    make_selector(cfg, store, snap, sail).on_exploration_finished(run_id)
    assert store.get_run(run_id)["state"] == "NONE_QUALIFIED"
    p = store.run_proposals(run_id)[0]
    assert store.exploration_state(p["exploration_id"]) == "DISCARDED"


def test_workers_are_not_test_run_before_combining(parts):
    cfg, store, snap, commit = parts
    sail = SelectionSail(proposal={"test_app": "fail"})
    run_id = seed_run(store)
    sel = make_selector(cfg, store, snap, sail)
    launches = []

    class Runs:
        def launch_synthesis(self, run, base_commit, plan, start_patch=None):
            launches.append(plan)
            return "ex-final"

    class Council:
        def critique_round(self, run_id, messages, candidates, current_code=""):
            return [], {}

        def chairman_directive(self, *args):
            return {"directive": "the plan", "aggregate": []}

    sel.runs = Runs()
    sel.council = Council()
    sel.on_exploration_finished(run_id)

    assert len(launches) == 1
    assert sail.boxes == {}
    assert store.exploration_state(store.run_proposals(run_id)[0]["exploration_id"]) \
        == "GATING"


def test_all_failed_when_no_proposals(parts):
    cfg, store, snap, commit = parts
    cfg.experiments.workers_max = 1
    sail = SelectionSail()
    executor = FakeExecutor()
    # A run whose single worker FAILED without producing a proposal.
    run_id = store.create_run("s1", 1, ["R1"])
    store.transition_run(run_id, "PLANNED", "seed")
    store.transition_run(run_id, "LAUNCHED", "seed")
    ex_id = store.create_exploration(
        "s1", "clean_rewrite", "pi", "t1", 1, {"target_ids": ["R1"]},
        run_id=run_id)
    store.transition_exploration(ex_id, "PROVISIONING", "seed")
    store.transition_exploration(ex_id, "RUNNING", "seed")
    store.transition_exploration(ex_id, "FAILED", "worker error")

    sel = make_selector(cfg, store, snap, sail, executor=executor)
    sel.on_exploration_finished(run_id)
    assert store.get_run(run_id)["state"] == "ALL_FAILED"


def test_a_crash_in_review_ends_the_run(parts):
    """An error during review must close the run. Left open, a held session waited
    out its full limit for a review that was never coming (2026-11-05)."""
    cfg, store, snap, commit = parts
    sail = SelectionSail(proposal={"test_app": "fail"})
    run_id = seed_run(store)
    sel = make_selector(cfg, store, snap, sail)

    class Council:
        def critique_round(self, *args, **kwargs):
            raise TypeError("sequence item 0: expected str instance, dict found")

    sel.council = Council()
    sel.on_exploration_finished(run_id)
    run = store.get_run(run_id)
    assert run["state"] == "ALL_FAILED"
    assert "selection failed" in run["exit_reason"]


def test_flaky_regressions_excluded(parts):
    cfg, store, snap, commit = parts
    sail = SelectionSail(proposal={"test_app": "fail"})
    run_id = seed_run(store, final=True)
    sel = make_selector(cfg, store, snap, sail, env=FakeEnv(flaky=["test_app"]))

    sel.on_exploration_finished(run_id)
    # test_app is flaky: its failure proves nothing, so no regression.
    assert store.get_run(run_id)["state"] == "READY_TO_DELIVER"


def test_regressions_pure_rules():
    assert _regressions({"test_a": "fail"}, {"test_a": "pass"}, set()) == ["test_a"]
    # Collection errors count as failing; skips are excluded entirely.
    assert _regressions({"test_a": "collection_error"}, {"test_a": "pass"},
                        set()) == ["test_a"]
    assert _regressions({"test_a": "skip"}, {"test_a": "pass"}, set()) == []
    # Flaky tests never count as regressions.
    assert _regressions({"test_a": "fail"}, {"test_a": "pass"}, {"test_a"}) == []




def _edit(path, old, new, header_old=1):
    return (f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n"
            f"@@ -{header_old},1 +{header_old},1 @@\n-{old}\n+{new}\n")


def test_docstring_and_metadata_edits_are_not_product_code():
    """A live run delivered a design that was only these, after six workers ran."""
    import base64

    from hindsight.selection import _touches_product_code as touches

    source = '"""search Python."""\nimport os\n'
    pre = {"a.py": base64.b64encode(source.encode()).decode()}
    assert not touches(_edit("a.py", '"""search Python."""', '"""search JS too."""'), pre)
    assert not touches(_edit("pyproject.toml", 'description = "a"', 'description = "b"'))
    assert not touches(_edit(".gitignore", "x", "y"))
    assert not touches(_edit("README.md", "x", "y"))
    assert not touches(_edit("docs/a.yaml", "x", "y"))
    assert not touches(_edit("uv.lock", "x", "y"))
    assert not touches(_edit("app.js", "// old", "// new"))


def test_a_real_code_change_is_product_code():
    import base64

    from hindsight.selection import _touches_product_code as touches

    source = '"""doc."""\nx = 1\n'
    pre = {"a.py": base64.b64encode(source.encode()).decode()}
    assert touches(_edit("a.py", "x = 1", "x = 2", header_old=2), pre)
    assert touches(_edit("app.js", "let a = 1", "let a = 2"))
    assert touches(_edit("config/rules.yaml", "a: 1", "a: 2"))
    new = ("diff --git a/b.py b/b.py\nnew file mode 100644\n--- /dev/null\n+++ b/b.py\n"
           "@@ -0,0 +1 @@\n+y = 1\n")
    assert touches(new)
    # Doesn't parse: not safe to call it unchanged.
    assert touches(_edit("a.py", "x = 1", "x = (", header_old=2), pre)
