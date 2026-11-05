"""Selection: a run's finished designs become one result to send.

Plain code, except where it calls the council. In order:

  1. Integrity already ran when each worker was collected; a design that failed
     it is not here.
  2. A design that changes no product code is dropped.
  3. Critics (if configured) and the chairman compare the designs with the current
     code. The chairman picks one design and writes a plan for finishing it from the
     user's verbatim messages, or decides nothing is worth sending.
  4. The final worker is the single finishing step. It starts from the chosen design's
     own code and base, and takes in new messages.
  5. The final design is tested on the code it was built on, with one retry. It is
     not merged onto newer code: code that moved on is the main agent's to reconcile,
     and delivery says how many messages behind the design is.

Nothing here shows a design is better, only that it broke nothing that was checked.

Only boxes tied to this run are ever terminated here: losers when a winner is
picked, all of them when a run ends without one.
"""

from __future__ import annotations

import base64
import io
import json
import logging
import re
import ast
import tarfile
import tempfile
import threading
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from .council import Council
    from .executor import Executor
    from .runs import RunManager

from .budget import BudgetManager
from .config import HindsightConfig
from .design_review import render_code
from .env import EnvManager
from .requirement_audit import render_user_messages
from .sail_client import SailClient, SailClientError
from .snapshot import Snapshotter
from .store import CostEntry, InvalidTransition, Store, built_on_key

log = logging.getLogger("hindsight")

_BOX_LIFETIME_SECONDS = 45 * 60
_CMD_TIMEOUT = 15 * 60
# A safety bound, not a policy: how many times one run may use a final worker.
MAX_FINAL_ROUNDS = 5
_CURRENT_CODE_CHARS = 100_000

_FAILING = {"fail", "collection_error"}
_DEAD_STATES = {"DISCARDED", "FAILED", "CANCELLED", "SUPERSEDED"}
_PATCH_DID_NOT_APPLY = "its patch did not apply"


def parse_junit(raw: bytes) -> dict[str, str]:
    """test name -> fail | pass | skip | collection_error. Pytest reports a test
    file it could not import as a testcase whose name ends in .py."""
    try:
        tree = ET.fromstring(raw)
    except ET.ParseError:
        return {}
    out: dict[str, str] = {}
    for tc in tree.iter("testcase"):
        name = tc.get("name") or ""
        if not name:
            continue
        if name.endswith(".py"):
            out[name] = "collection_error"
            continue
        children = {child.tag for child in tc}
        if "failure" in children or "error" in children:
            out[name] = "fail"
        elif "skipped" in children:
            out[name] = "skip"
        else:
            out[name] = "pass"
    return out


class Selector:
    def __init__(
        self,
        cfg: HindsightConfig,
        store: Store,
        sail: SailClient,
        env: EnvManager,
        snapshotter: Snapshotter,
        executor: "Executor",
        budget: BudgetManager,
    ):
        self.cfg = cfg
        self.store = store
        self.sail = sail
        self.env = env
        self.snapshotter = snapshotter
        self.executor = executor
        self.budget = budget
        # Wired by the orchestrator after construction (circular refs).
        self.council: "Council | None" = None
        self.runs: "RunManager | None" = None
        self._lock = threading.Lock()
        self._selecting: set[str] = set()
        self._recheck: set[str] = set()

    # -------------------------------------------------------------- trigger

    def on_exploration_finished(self, run_id: str | None) -> None:
        """Called by the executor when a run worker reaches a resting state."""
        self._try_select(run_id)

    def resume(self) -> None:
        """After an orchestrator restart: re-arm selection for runs whose
        workers all rest (LAUNCHED with nothing in flight) or that were
        mid-selection when we died (SELECTING, nothing in flight)."""
        for row in self.store._query(
                "SELECT id FROM runs WHERE state IN ('LAUNCHED','SELECTING')"):
            self._try_select(row["id"])

    def _try_select(self, run_id: str | None, depth: int = 0) -> None:
        if not run_id or depth > 10:
            return
        with self._lock:
            if run_id in self._selecting:
                # A worker finished while selection runs; recheck after the
                # current round completes.
                self._recheck.add(run_id)
                return
            run = self.store.get_run(run_id)
            if run is None:
                return
            if run["state"] not in ("LAUNCHED", "SELECTING"):
                return
            if self.store.run_pending_count(run_id) > 0:
                return
            if run["state"] == "LAUNCHED":
                try:
                    self.store.transition_run(run_id, "SELECTING",
                                              "all workers finished")
                except InvalidTransition:
                    return  # another thread is selecting
            self._selecting.add(run_id)
        try:
            self._select_run(run_id)
        except Exception as e:  # noqa: BLE001
            # An unexpected error must end the run, not leave it in review forever:
            # a held session would otherwise wait out its whole limit (2026-11-05).
            log.exception("run %s: selection failed", run_id)
            run = self.store.get_run(run_id)
            if run is not None and run["state"] == "SELECTING":
                self.end_run(run_id, "ALL_FAILED",
                             f"selection failed: {type(e).__name__}: {e}"[:300])
        finally:
            with self._lock:
                self._selecting.discard(run_id)
                recheck = run_id in self._recheck
                self._recheck.discard(run_id)
        if recheck:
            self._try_select(run_id, depth + 1)

    # ------------------------------------------------------------ selection

    def _select_run(self, run_id: str) -> None:
        run = self.store.get_run(run_id)
        if run is None or run["state"] != "SELECTING":
            return
        session_id = run["session_id"]
        base_commit = self._base_commit(session_id)
        if base_commit is None:
            self.end_run(run_id, "ALL_FAILED",
                         "no usable snapshot to select against")
            return

        proposals = [p for p in self.store.run_proposals(run_id)
                     if p["exploration_state"] not in _DEAD_STATES]
        if self._final_rounds(run_id) > 0:
            self._check_final(run, base_commit, proposals)
            return

        # --- the workers are in: combine whatever changed product code ------
        designs = []
        for p in proposals:
            if _touches_product_code(p.get("patch") or "", p.get("preimages")):
                designs.append(p)
                continue
            reason = "worker found no product code to change"
            self.store.set_proposal_selection(p["id"], False, [], [])
            self.store.save_gate_result(p["id"], "2_audit_gate", False,
                                        {"no_change": True}, reason)
            self.store.transition_exploration(
                p["exploration_id"], "DISCARDED", reason)
        if not proposals:
            self.end_run(run_id, "ALL_FAILED", "all workers failed or discarded")
        elif not designs:
            self.end_run(run_id, "NONE_QUALIFIED", "no worker changed product code")
        else:
            self._combine(run, base_commit, designs)

    def _check_final(self, run, base_commit: str, proposals: list[dict]) -> None:
        """A last look at what is about to be sent. A failure gets one more try,
        with the failures. A test can fail only because it was tied to the old
        structure, and code can't tell that from a real bug, so a design whose tests
        ran and still fail goes out with the failures listed, for the main agent to
        judge. Only a design that could not be run at all sends nothing."""
        run_id = run["id"]
        final = next((p for p in reversed(proposals) if self._is_final(p)), None)
        if final is None:
            self.end_run(run_id, "NONE_QUALIFIED", "the final worker left no design")
            return
        if not _touches_product_code(final.get("patch") or "", final.get("preimages")):
            self.store.transition_exploration(
                final["exploration_id"], "DISCARDED", "no product-code change")
            self.end_run(run_id, "NONE_QUALIFIED",
                         "the final worker found no product code to change")
            return
        # Checked on the code it was built on. The latest code may have moved on, and
        # the main agent reconciles that: it can use the ideas even if the patch
        # would no longer apply.
        built_on = self.store.kv_get(built_on_key(final["exploration_id"]))
        if not built_on:
            self.store.transition_exploration(
                final["exploration_id"], "DISCARDED", "unknown base commit")
            self.end_run(run_id, "NONE_QUALIFIED", "the final design has no recorded base")
            return
        base_commit = built_on
        current = (self._current_matrix(run, base_commit)
                   if self.cfg.project.test_command else {})
        if current:
            flaky = set(self.env.ensure_env(base_commit).flaky_tests or [])
            outcomes = self._proposal_matrix(final, base_commit)
            passed = self._audit_gate(final, outcomes, current, flaky,
                                      _regressions(outcomes, current, flaky))
        else:
            passed = self._verify_without_suite(final, base_commit)
        if passed:
            self._deliver_winner(run, final, "final design passed the check")
            return
        key = f"final_retried:{run_id}"
        retried = bool(self.store.kv_get(key))
        gate = self.store.gate_features(final["id"], "2_audit_gate")
        if retried and gate.get("tests_ran"):
            self._deliver_winner(run, final, "sent with the failures listed: its "
                                 "tests still fail after the retry")
            return
        self.store.transition_exploration(
            final["exploration_id"], "DISCARDED", "final design failed its check")
        if retried:
            self.end_run(run_id, "NONE_QUALIFIED", "final design could not be run")
            return
        self.store.kv_set(key, "1")
        self._finish_one(run, base_commit, final,
                         failures=gate.get("regressions") or [])

    def _verify_without_suite(self, p: dict, base_commit: str) -> bool:
        """No project test results to compare against. The design must build and
        pass its worker's own tests, which is weaker, so delivery says so. The
        worker must have written tests, unless its command is the project's own
        (one that reports no per-test results, like `go test`)."""
        project_cmd = self.cfg.project.test_command
        summary = p.get("summary")
        cmd = ((summary.get("verify_command") or "") if isinstance(summary, dict)
               else "").strip()
        failure = ""
        output = ""
        ran = False
        if not cmd:
            failure = "no verify_command given"
        elif cmd != project_cmd and not _touches_tests(p.get("patch") or ""):
            failure = "it adds no tests of its own"
        else:
            failure, output = self._run_verify(p, base_commit, cmd)
            ran = failure != _PATCH_DID_NOT_APPLY
        qualified = not failure
        reason = (f"no project tests; passed its own: {cmd}" if qualified
                  else f"no project tests; {failure}")
        self.store.set_proposal_selection(p["id"], qualified,
                                          [failure] if failure else [], [])
        self.store.save_gate_result(
            p["id"], "2_audit_gate", qualified,
            {"no_project_suite": True, "verify_command": cmd, "tests_ran": ran,
             "regressions": [failure] if failure else [], "output_tail": output,
             "tests_rewritten": self.store.gate_features(
                 p["id"], "0_integrity").get("allowed_test_changes", [])},
            reason)
        return qualified

    def _run_verify(self, p: dict, base_commit: str, cmd: str) -> tuple[str, str]:
        """Returns (what went wrong or "", output tail)."""
        box_id, t0 = self._fresh_box(f"hs-verify-{p['id']}", base_commit)
        try:
            self.sail.write_files(box_id, {"/work/patch.diff": p["patch"]})
            self._bridge_blobs(box_id, p)
            r = self.sail.run(box_id, ["bash", "-c",
                                       "cd /repo && git apply --3way /work/patch.diff"],
                              timeout=_CMD_TIMEOUT)
            if r.exit_code != 0:
                return _PATCH_DID_NOT_APPLY, (r.stderr or "")[-2000:]
            r = self.sail.run(box_id, ["bash", "-c", f"cd /repo && {cmd}"],
                              timeout=_CMD_TIMEOUT)
            tail = ((r.stdout or "") + (r.stderr or ""))[-2000:]
            if r.exit_code != 0:
                return f"`{cmd}` exited {r.exit_code}: {tail[-600:]}", tail
            return "", tail
        finally:
            self._finish_box(f"verify-{p['id']}", t0, box_id)

    # ---------------------------------------------------------- audit gate

    def _audit_gate(self, p: dict, prop: dict[str, str],
                    current: dict[str, str], flaky: set[str],
                    regressions: list[str]) -> bool:
        """Qualification. Nothing here shows the proposal is BETTER, only that it broke
        nothing that was checked: no flip exists to require, and a worker's
        own tests cannot stand in for one. They are recorded and reported.

        A test can also escape `_classify` by disappearing from the proposal,
        so a test that passes today and vanished counts as a regression unless
        stage 0 excused its removal with the user's own words."""
        excused = {c["test"] for c in self.store.gate_features(
            p["id"], "0_integrity").get("allowed_test_changes", [])}
        vanished = sorted(
            f"{name} (vanished)" for name, o in current.items()
            if o == "pass" and name not in prop and name not in flaky
            and name not in excused) if prop else []
        all_regressions = regressions + vanished
        product = _touches_product_code(p.get("patch") or "", p.get("preimages"))
        qualified = not all_regressions and bool(prop) and product
        reason = (
            "no regressions in the existing suite; nothing shows it is better"
            if qualified else
            f"{len(all_regressions)} regression(s)" if all_regressions else
            "no product-code change" if not product else
            "no executed test evidence")
        new_tests = {n: o for n, o in prop.items() if n not in current}
        self.store.set_proposal_selection(p["id"], qualified, all_regressions, [])
        self.store.save_gate_result(
            p["id"], "2_audit_gate", qualified,
            {"regressions": all_regressions,
             "tests_ran": bool(prop),
             "n_proposal_tests": len(prop),
             "n_current_tests": len(current),
             "existing_tests_still_passing": sum(
                 1 for n, o in current.items()
                 if o == "pass" and prop.get(n) == "pass" and n not in excused),
             # Executed, on tests the worker was not allowed to quietly edit.
             "existing_failures_fixed": sorted(
                 n for n, o in current.items()
                 if o in _FAILING and prop.get(n) == "pass"
                 and n not in flaky and n not in excused),
             "worker_tests_unverified": {
                 "passed": sorted(n for n, o in new_tests.items() if o == "pass"),
                 "not_passed": sorted(n for n, o in new_tests.items() if o != "pass"),
             },
             "tests_rewritten": self.store.gate_features(
                 p["id"], "0_integrity").get("allowed_test_changes", [])},
            reason)
        return qualified

    # --------------------------------------------------------------- deciding

    def _finish_one(self, run, base_commit: str, p: dict,
                    failures: Optional[list[str]] = None) -> None:
        """One design to take forward: the final worker continues from its code, so
        what gets sent has seen the newest messages."""
        plan = "Finish this design. Where it already does what the user asked, leave it.\n\n"
        if failures:
            plan += ("Its last version broke these tests. Fix them, or if a test only "
                     "depends on the old structure, change it and list it under "
                     "tests_changed:\n" + "\n".join(failures) + "\n\n")
        plan += "ITS SUMMARY:\n" + json.dumps(p.get("summary"), default=str)[:6000]
        built_on = self.store.kv_get(built_on_key(p["exploration_id"])) or base_commit
        if not self._launch_final(run, built_on, plan, p.get("patch")):
            self.end_run(run["id"], "NONE_QUALIFIED",
                         "the final worker could not be launched")

    def _combine(self, run, base_commit: str, candidates: list[dict]) -> None:
        """Critics and the chairman choose a design and write the plan for finishing it
        from the user's verbatim messages, or decide no design beats the current code.
        Even a lone design goes through this, because workers no longer get to say the
        code should stay as it is."""
        run_id = run["id"]
        messages = render_user_messages(
            self.store.user_text_for_session(run["session_id"]))
        candidates = [{**p, "worker_tests": self.store.gate_features(
            p["id"], "1_worker_tests")} for p in candidates]
        chairman = None
        if self.council is not None:
            current = self._current_product_code(run["session_id"], base_commit)
            critiques, labels = self.council.critique_round(
                run_id, messages, candidates, current)
            chairman = self.council.chairman_directive(
                run_id, messages, candidates, critiques, labels, current)
        if chairman is None:
            self.end_run(run_id, "NONE_QUALIFIED", "the chairman was unavailable")
            return
        self.store.set_run_chairman(run_id, chairman)
        for a in chairman.get("aggregate") or []:
            self.store.save_gate_result(
                a["proposal_id"], "4_council_rank", True,
                {"average_rank": a["average_rank"],
                 "rankings_count": a["rankings_count"],
                 "n_critiques": len(critiques)},
                "aggregate peer ranking")
        if chairman.get("no_change"):
            self.end_run(run_id, "NONE_QUALIFIED",
                         "the chairman found no design better than the current code: "
                         + chairman["rationale"][:300])
            return
        chosen = next((p for p in candidates
                       if p["id"] == chairman.get("chosen_proposal_id")), candidates[0])
        built_on = self.store.kv_get(built_on_key(chosen["exploration_id"])) or base_commit
        if not self._launch_final(run, built_on, chairman["directive"], chosen.get("patch")):
            self.end_run(run_id, "NONE_QUALIFIED",
                         "the final worker could not be launched")

    def _current_product_code(self, session_id: str, commit: str) -> str:
        """What critics compare the designs with. Tests and metadata are left out,
        since they would crowd out the code."""
        return render_code(
            self.snapshotter, self.store, session_id, commit, _CURRENT_CODE_CHARS,
            keep=lambda path: not (_is_test_path(path) or _is_metadata_path(path)))

    def _launch_final(self, run, base_commit: str, plan: str,
                      start_patch: Optional[str] = None) -> bool:
        if self._final_rounds(run["id"]) >= MAX_FINAL_ROUNDS:
            log.warning("run %s: final worker limit reached", run["id"])
            return False
        return self.runs.launch_synthesis(run, base_commit, plan, start_patch) is not None

    def _final_rounds(self, run_id: str) -> int:
        return sum(1 for e in self.store.run_explorations(run_id)
                   if _budget_of(e).get("synthesis"))

    def _is_final(self, p: dict) -> bool:
        ex = self.store.get_exploration(p["exploration_id"])
        return ex is not None and bool(_budget_of(ex).get("synthesis"))

    # -------------------------------------------------------------- send back

    def send_back(self, run, proposal: dict, messages: list[dict]) -> None:
        """The user said something after the winner finished. It goes back to that
        same worker, which still has its context, rather than back to the council.
        Its new result then passes the gate like any other."""
        run_id = run["id"]
        session_id = run["session_id"]
        if self._final_rounds(run_id) >= MAX_FINAL_ROUNDS or \
                not self.budget.check(
                    self.budget.estimate_strategy("council_synthesis")).ok:
            self.store.transition_exploration(
                proposal["exploration_id"], "SUPERSEDED", "newer user message")
            self.end_run(run_id, "DROPPED_STALE",
                         "newer user message, and no further final round allowed")
            return
        old = self.store.get_exploration(proposal["exploration_id"])
        new_id = None
        try:
            if old["box_id"]:
                self.sail.wake(old["box_id"])
                new_id = self.executor.submit_followup(old, messages)
        except SailClientError:
            log.warning("run %s: could not wake %s; starting a new final worker",
                        run_id, old["box_id"])
        self.store.transition_run(run_id, "SELECTING", "newer user message")
        self.store.transition_exploration(
            proposal["exploration_id"], "SUPERSEDED", "newer user message")
        if new_id is not None:
            self.executor.start_followup(new_id, messages)
            return
        base_commit = self._base_commit(session_id)
        self._finish_one(run, base_commit, proposal)

    # ---------------------------------------------------------------- endings

    def _deliver_winner(self, run, winner: dict, reason: str) -> None:
        run_id = run["id"]
        self.store.transition_exploration(
            winner["exploration_id"], "NUDGE_READY", "selected for delivery")
        for e in self.store.run_explorations(run_id):
            if e["id"] != winner["exploration_id"] and e["state"] == "GATING":
                self.store.transition_exploration(
                    e["id"], "DISCARDED", "not the final design")
        keep = (self.store.get_exploration(winner["exploration_id"]) or {})["box_id"]
        self._terminate_run_boxes(run_id, keep=keep)
        self.store.set_run_winner(run_id, winner["id"])
        self.store.transition_run(run_id, "READY_TO_DELIVER",
                                  f"winner {winner['id']}: {reason}")
        log.info("selection: run %s winner %s (%s)", run_id, winner["id"], reason)

    def end_run(self, run_id: str, state: str, reason: str) -> None:
        """The run ends with nothing to send: free every box tied to it."""
        for e in self.store.run_explorations(run_id):
            if e["state"] == "GATING":
                self.store.transition_exploration(e["id"], "DISCARDED", reason[:200])
        self._terminate_run_boxes(run_id)
        self.store.transition_run(run_id, state, reason)

    def _terminate_run_boxes(self, run_id: str, keep: Optional[str] = None) -> None:
        for box_id in {e["box_id"] for e in self.store.run_explorations(run_id)
                       if e["box_id"] and e["box_id"] != keep}:
            try:
                self.sail.terminate(box_id)
            except SailClientError:
                log.warning("could not terminate %s; the reaper will retry", box_id)

    # ------------------------------------------------------------- helpers

    def _base_commit(self, session_id: str) -> Optional[str]:
        base = self.store.latest_usable_snapshot(session_id)
        if base is not None:
            return base["post_snapshot"]
        turn = self.store.latest_turn(session_id)
        return turn["post_snapshot"] if turn else None

    # ------------------------------------------------------------- box work

    def _fresh_box(self, name: str, base_commit: str) -> tuple[str, float]:
        """Env-checkpoint box with the latest snapshot extracted at /repo
        and committed (so --3way has blobs to merge against)."""
        env = self.env.ensure_env(base_commit)
        box = self.sail.create_box(
            name, from_checkpoint_id=env.checkpoint_id,
            max_lifetime_seconds=_BOX_LIFETIME_SECONDS)
        t0 = time.time()
        try:
            with tempfile.NamedTemporaryFile(suffix=".tar.gz", delete=False) as tf:
                tarball = Path(tf.name)
            try:
                self.snapshotter.export_tarball(base_commit, tarball)
                self.sail.upload_file(box.id, str(tarball), "/work/repo.tar.gz")
            finally:
                tarball.unlink(missing_ok=True)
            r = self.sail.run(box.id, ["bash", "-c", (
                "rm -rf /repo && mkdir -p /repo /work && "
                "tar -xzf /work/repo.tar.gz -C /repo && "
                "cd /repo && git init -q && git add -A && "
                "git -c user.name=hindsight -c user.email=h@localhost "
                "commit -qm baseline")], timeout=600)
            if r.exit_code != 0:
                raise SailClientError(f"selection baseline failed: {r.stderr[-300:]}")
        except Exception:
            self.sail.terminate(box.id)
            raise
        return box.id, t0

    def _finish_box(self, ref: str, t0: float, box_id: str) -> None:
        try:
            self.sail.terminate(box_id)
        finally:
            elapsed = time.time() - t0
            self.store.record_cost(CostEntry(
                ref=ref, kind="box", box_seconds=elapsed,
                usd=0.002 * elapsed / 60))

    def _bridge_blobs(self, box_id: str, proposal: dict) -> None:
        """Write the proposal's baseline pre-images into the box's object
        store, so `git apply --3way` gets a real ancestor and can MERGE
        instead of falling back to literal context matching. Without this,
        ordinary drift in the same file (the user/main agent keeps coding
        while workers run) fails exactly like a true conflict and the
        proposal is discarded (48/48). True
        line-level semantic overlap still — by design — drops the patch."""
        preimages = proposal.get("preimages") or {}
        if not preimages:
            return
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            for path, b64 in preimages.items():
                data = base64.b64decode(b64)
                info = tarfile.TarInfo(f"preimages/{path}")
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
        with tempfile.NamedTemporaryFile(suffix=".tar.gz", delete=False) as tf:
            tf.write(buf.getvalue())
            tmp = Path(tf.name)
        try:
            self.sail.upload_file(box_id, str(tmp), "/work/preimages.tar.gz")
        finally:
            tmp.unlink(missing_ok=True)
        self.sail.run(box_id, ["bash", "-c",
                               "rm -rf /work/preimages && mkdir -p /work/preimages"
                               " && tar -xzf /work/preimages.tar.gz -C /work"
                               " && cd /work/preimages"
                               " && find . -type f -exec git --git-dir=/repo/.git"
                               " hash-object -w {} +"],
                      timeout=_CMD_TIMEOUT)

    def _run_matrix(self, box_id: str) -> dict[str, str]:
        """The project's test suite. A non-pytest command leaves no junit, so an
        honest empty result."""
        cmd = self.cfg.project.test_command
        if not cmd:
            return {}
        flags = (" --junitxml=/work/proj_junit.xml -p no:cacheprovider"
                 if "pytest" in cmd else "")
        self.sail.run(box_id, ["bash", "-c", f"cd /repo && {cmd}{flags}"],
                      timeout=_CMD_TIMEOUT)
        try:
            return parse_junit(self.sail.read_file(box_id, "/work/proj_junit.xml"))
        except SailClientError:
            return {}

    def _current_matrix(self, run, base_commit: str) -> dict[str, str]:
        """Run the suite on the CURRENT code (one shared box per round)."""
        box_id, t0 = self._fresh_box(f"hs-cur-{run['id']}", base_commit)
        try:
            return self._run_matrix(box_id)
        finally:
            self._finish_box(f"matrix-current-{run['id'][:10]}", t0, box_id)

    def _proposal_matrix(self, proposal: dict, base_commit: str) -> dict[str, str]:
        """The suite on the REBASED proposal (fresh box, patch applied)."""
        p_id = proposal["id"]
        box_id, t0 = self._fresh_box(f"hs-matrix-{p_id}", base_commit)
        try:
            self.sail.write_files(box_id, {"/work/patch.diff": proposal["patch"]})
            self._bridge_blobs(box_id, proposal)
            r = self.sail.run(box_id, ["bash", "-c",
                                      "cd /repo && git apply --3way /work/patch.diff"],
                              timeout=_CMD_TIMEOUT)
            if r.exit_code != 0:
                return {}  # honest empty: the matrix could not run
            return self._run_matrix(box_id)
        finally:
            self._finish_box(f"matrix-{p_id}", t0, box_id)


def _budget_of(exploration) -> dict:
    try:
        return json.loads(exploration["budget_json"] or "{}")
    except ValueError:
        return {}


def _regressions(prop: dict[str, str], current: dict[str, str],
                 flaky: set[str]) -> list[str]:
    return sorted(
        name for name, o in prop.items()
        if o in _FAILING and current.get(name) == "pass" and name not in flaky)


def _is_test_path(path: str) -> bool:
    base = path.rsplit("/", 1)[-1]
    stem = base.rsplit(".", 1)[0]
    parts = path.split("/")[:-1]
    return (stem.startswith("test_") or stem.startswith("test-") or stem == "test"
            or stem.endswith("_test") or stem.endswith(".test")
            or stem.endswith("_spec") or base.endswith(".bats")
            or any(d in ("test", "tests", "__tests__", "testdata") for d in parts))


def _patch_paths(patch: str) -> set[str]:
    paths = set(re.findall(r"^diff --git a/(\S+) b/\S+$", patch, re.M))
    return paths | set(re.findall(r"^--- a/(\S+)$", patch, re.M))


def product_patch(patch: str) -> str:
    """The patch without tests and project metadata: the design itself."""
    kept = []
    for section in re.split(r"(?m)^(?=diff --git )", patch):
        found = re.match(r"diff --git a/(\S+) b/\S+", section)
        if found and not (_is_test_path(found.group(1)) or _is_metadata_path(found.group(1))):
            kept.append(section)
    return "".join(kept)


def _touches_tests(patch: str) -> bool:
    return any(_is_test_path(p) for p in _patch_paths(patch))


# Project metadata and prose. Editing only these isn't a design.
_NOT_PRODUCT_NAMES = {
    ".gitignore", ".gitattributes", ".dockerignore", ".editorconfig", "pyproject.toml",
    "setup.cfg", "package.json", "package-lock.json", "yarn.lock", "pnpm-lock.yaml",
    "uv.lock", "poetry.lock", "pipfile", "pipfile.lock", "cargo.toml", "cargo.lock",
    "go.mod", "go.sum", "gemfile", "gemfile.lock", "conftest.py",
}
_NOT_PRODUCT_SUFFIXES = (".md", ".rst", ".txt", ".lock")
_COMMENT_PREFIXES = ("#", "//", "/*", "*", "<!--", "--")


def _is_metadata_path(path: str) -> bool:
    base = path.rsplit("/", 1)[-1].lower()
    return (base in _NOT_PRODUCT_NAMES or base.endswith(_NOT_PRODUCT_SUFFIXES)
            or base.startswith(("license", "changelog", "readme"))
            or "docs" in path.lower().split("/")[:-1])


def _file_diffs(patch: str) -> dict[str, list[str]]:
    files: dict[str, list[str]] = {}
    current: list[str] | None = None
    for line in patch.splitlines():
        git = re.match(r"^diff --git a/(\S+) b/\S+$", line)
        plain = re.match(r"^--- a/(\S+)$", line)
        if git or (plain and plain.group(1) not in files):
            current = files.setdefault((git or plain).group(1), [])
        elif current is not None:
            current.append(line)
    return files


def _apply_hunks(old: str, diff: list[str]) -> str:
    """The new text of one file. Patches here were made by git from this same text."""
    source = old.split("\n")
    out: list[str] = []
    at = 0
    in_hunk = False
    for line in diff:
        hunk = re.match(r"^@@ -(\d+)(?:,(\d+))? \+\d+(?:,\d+)? @@", line)
        if hunk:
            start = int(hunk.group(1)) - (0 if hunk.group(2) == "0" else 1)
            out += source[at:start]
            at = start
            in_hunk = True
        elif in_hunk and line.startswith("+"):
            out.append(line[1:])
        elif in_hunk and line.startswith("-"):
            at += 1
        elif in_hunk and line.startswith(" "):
            out.append(source[at] if at < len(source) else line[1:])
            at += 1
    return "\n".join(out + source[at:])


def _python_code(text: str) -> str | None:
    """The file's code with comments and docstrings gone. None if it doesn't parse."""
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return None
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                             ast.AsyncFunctionDef)) and node.body:
            first = node.body[0]
            if (isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant)
                    and isinstance(first.value.value, str)):
                node.body = node.body[1:]
    return ast.dump(tree)


def _changes_code(path: str, diff: list[str], preimage: str | None) -> bool:
    changed = [line[1:] for line in diff
               if line[:1] in "+-" and not line.startswith(("+++", "---"))]
    if not changed:
        return False
    if path.endswith(".py"):
        new_file = any(line.startswith("new file") for line in diff)
        if new_file or preimage is not None:
            old = "" if new_file else preimage
            before, after = _python_code(old), _python_code(_apply_hunks(old, diff))
            if before is not None and after is not None:
                return before != after
    return any(line.strip() and not line.strip().startswith(_COMMENT_PREFIXES)
               for line in changed)


def _touches_product_code(patch: str, preimages: dict | None = None) -> bool:
    """True if the patch changes code outside tests. Comments, docstrings, project
    metadata and prose don't count: a design made only of those has changed nothing
    about how the code works. `preimages` is {path: base64} of the original files."""
    for path, diff in _file_diffs(patch).items():
        if _is_test_path(path) or _is_metadata_path(path):
            continue
        raw = (preimages or {}).get(path)
        try:
            preimage = base64.b64decode(raw).decode() if raw else None
        except ValueError:
            preimage = None
        if _changes_code(path, diff, preimage):
            return True
    return False
