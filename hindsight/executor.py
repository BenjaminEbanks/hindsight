"""Exploration executor (Sections 6.7-6.8): the background-agent lifecycle.

For each exploration: ensure env -> create box from checkpoint -> upload
snapshot + task + driver -> start driver with nohup -> poll status.json ->
collect result.json -> gate stage 0 -> record proposal -> terminate box.

The orchestrator only polls; the in-box driver does all the agent-running,
so this survives orchestrator restarts, laptop sleep, and network blips.
"""

from __future__ import annotations

import base64
import io
import json
import logging
import tarfile
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import TYPE_CHECKING

from .config import HindsightConfig
from .env import EnvManager
from .planner import PlannedExploration
from .prompts import load, render
from .requirement_audit import check_audit_integrity, render_user_messages
from .resume import agent_session_of
from .sail_client import SailClient, SailClientError

if TYPE_CHECKING:
    from .selection import Selector
from .snapshot import Snapshotter, SecretsFound
from .store import Store, built_on_key

log = logging.getLogger("hindsight")

# Worker-resume checkpoints: 30-day TTL.
_RESUME_CHECKPOINT_TTL = 30 * 24 * 3600
_MAX_TRANSCRIPT_CHARS = 2_000_000

# What every worker is asked to do. Someone else decides whether a redesign is worth using,
# so a worker doesn't get to decide that nothing should change: told it could, all six on
# one task did, and few on any task changed the structure.
REDESIGN_TASK = """Your task: build the design this code should have had, knowing everything the
user has now asked for.
1. List each request the user made. Later messages override earlier ones. Read the code
   and note workarounds, assumptions the requests have made wrong, and anything that
   doesn't do what was asked.
2. Restructure the code into your design. This is the point of your work: change how the
   code is organized, such as its parts, its data model or where its state lives. That
   the code already works only tells you which behavior to keep. Handing back the same
   structure with a few fixes is not a result, and neither is the same code moved into new
   files. Keep the behavior and interfaces the user's messages fix, including any file,
   entry point or command they name, and aim for simpler code: don't add layers the
   requests don't need.
3. Fix anything that doesn't do what the user asked.
4. Write at least one test for each request, in the repo's existing test layout, that
   checks what the user actually asked for.
5. In your summary, say plainly what the new structure makes better and what it costs.
   Others will compare it with the current code, so a design that turns out not to be
   worth using is a useful result."""

# The final worker builds the plan it was given. It doesn't start its own redesign.
FINAL_TASK = """Your task: finish the chosen redesign. It is already in /repo, uncommitted on
top of the code it was built from, so `git diff` shows it. Don't start again.
1. List each request the user made. Later messages override earlier ones.
2. Make the changes in the plan at the end of this message, and fix anything that doesn't
   do what the user asked. Keep the design's structure: don't move its code back or
   reduce it to the old structure in new files.
3. Make sure each request has a test, in the repo's existing test layout, that checks what
   the user actually asked for.
4. In your summary, say concretely what the new structure makes better than the old one,
   such as which future change now touches less code, and what it costs. If you can't
   name a real gain, say so."""

# Each worker builds one structure. The auditor chooses the directions from the requests
# and the code; the open worker gets none, to cover what the auditor missed.
STRATEGY_INSTRUCTIONS = {
    "direction": (
        "YOUR DIRECTION, chosen for you from the user's requests:\n{direction}\n\n"
        "Build this structure. Change its details where the code shows a better way, "
        "but don't drift back to the current structure."
    ),
    "open_redesign": (
        "Nobody has chosen a direction for you. Design this as if you had known "
        "everything the user asked for from the start, and build that. Prefer the "
        "simplest design that does what they asked."
    ),
    # The final worker: the single finishing step of a run.
    "council_synthesis": (
        "You are the final engineer on this change. Follow the plan below, but the "
        "user's newest messages win: where anything the user said, above or in "
        "/work/updates, disagrees with the plan, do what the user said. Check "
        "/work/updates once more immediately before you finish, and take in anything "
        "new. Existing tests must keep passing, unless they contradict "
        "what the user said or only depend on the old structure; list any you "
        "change under tests_changed.\n\n"
        "PLAN:\n{directive}"
    ),
}

UNFINISHED_SUMMARY = ("This engineer ran out of time, so this is its code as it stood. "
                      "It may be incomplete, and it wrote no description of its work.")

FOLLOWUP_PROMPT = (
    "The user said more after you finished. Their newest messages win: update your "
    "work in /repo so it does what they now ask, and leave the rest of it alone. "
    "If a message changes nothing about this work, leave the code as it is and say "
    "so. Check /work/updates once more immediately before you finish. End with the "
    "same JSON summary block as before, describing the work as it now stands.\n\n"
    "THE NEW MESSAGES:\n{messages}"
)


def render_audit_worker_prompt(user_messages: list[str], strategy: str,
                               test_command: str, **extra: str) -> str:
    key = "direction" if strategy.startswith("direction_") else strategy
    instruction = STRATEGY_INSTRUCTIONS.get(key, "").format(**extra)
    preamble = render(
        load("worker_preamble_audit"),
        task=FINAL_TASK if strategy == "council_synthesis" else REDESIGN_TASK,
        user_messages=render_user_messages(user_messages),
        test_command=test_command)
    return preamble + "\n\n" + instruction if instruction else preamble


class Executor:
    def __init__(self, cfg: HindsightConfig, store: Store, snapshotter: Snapshotter,
                 sail: SailClient, env_manager: EnvManager,
                 selection: "Selector | None" = None):
        self.cfg = cfg
        self.store = store
        self.snapshotter = snapshotter
        self.sail = sail
        self.env = env_manager
        # Run workers rest in GATING until selection decides. Wired after
        # construction (circular ref).
        self.selection = selection
        self.pool = ThreadPoolExecutor(
            max_workers=cfg.explore.max_concurrent_boxes, thread_name_prefix="hs-ex")

    # ---------------------------------------------------------------- public

    def submit(self, plan: PlannedExploration, session_id: str,
               base_commit: str, harness: str = "opencode", *,
               samples: int | None = None, run_id: str | None = None,
               extra_prompt: dict[str, str] | None = None,
               mark_synthesis: bool = False,
               start_patch: str | None = None) -> list[str]:
        """Create N sample explorations for a plan and run them in the pool.

        extra_prompt carries strategy-instruction placeholders (the final
        worker gets its plan this way); mark_synthesis tags the exploration's
        budget so selection recognizes it on return. start_patch is a design,
        made on base_commit, that the worker's /repo starts with."""
        n = samples if samples is not None else self.cfg.explore.samples_per_strategy
        ids = []
        for _ in range(n):
            budget = {"target_ids": plan.target_ids,
                      "max_wall_clock_s": self.cfg.explore.worker_wall_clock_minutes * 60,
                      "strategy": plan.strategy}
            if mark_synthesis:
                budget["synthesis"] = True
            if (extra_prompt or {}).get("direction"):
                budget["direction"] = extra_prompt["direction"]
            ex_id = self.store.create_exploration(
                session_id=session_id, strategy=plan.strategy, harness=harness,
                base_turn_id=plan.base_turn_id, ledger_version=0,
                budget=budget,
                run_id=run_id,
            )
            self.store.kv_set(built_on_key(ex_id), base_commit)
            ids.append(ex_id)
            self.pool.submit(self._run, ex_id, plan, base_commit,
                             harness, extra_prompt or {}, start_patch)
        return ids

    def submit_followup(self, old, messages: list[dict]) -> str:
        """Record a follow-up round for a finished worker whose box is awake: same
        box, same agent conversation. Started by start_followup."""
        budget = {**json.loads(old["budget_json"] or "{}"), "synthesis": True,
                  "agent_session": agent_session_of(old)}
        ex_id = self.store.create_exploration(
            session_id=old["session_id"], strategy=old["strategy"],
            harness=old["harness"], base_turn_id=old["base_turn_id"],
            ledger_version=0, budget=budget, run_id=old["run_id"])
        self.store.set_box_id(ex_id, old["box_id"])
        self.store.kv_set(f"updates_wm:{ex_id}", str(messages[-1]["id"]))
        self.store.kv_set(built_on_key(ex_id), self.store.kv_get(built_on_key(old["id"])) or "")
        return ex_id

    def start_followup(self, ex_id: str, messages: list[dict]) -> None:
        self.pool.submit(self._run_followup, ex_id, messages)

    def resume(self) -> int:
        """After a crash/restart, reattach to RUNNING explorations by box id."""
        n = 0
        for row in self.store.live_explorations():
            if row["state"] == "RUNNING" and row["box_id"]:
                plan = PlannedExploration(
                    strategy=row["strategy"], base_turn_id=row["base_turn_id"],
                    base_turn_n=0,
                    target_ids=json.loads(row["budget_json"] or "{}").get("target_ids", []))
                self.pool.submit(self._monitor_only, row["id"])
                n += 1
            elif row["state"] in ("PENDING", "PROVISIONING"):
                self.store.transition_exploration(row["id"], "FAILED", "orchestrator restart")
        return n

    # ------------------------------------------------------------- internals

    def _task_json(self, ex_id: str, plan: PlannedExploration,
                   harness: str, baseline_commit: str, session_id: str = "",
                   extra_prompt: dict[str, str] | None = None) -> dict:
        # Everything up to this message is in the prompt; later ones are streamed.
        self.store.kv_set(f"updates_wm:{ex_id}",
                          str(self.store.last_user_message_id(session_id)))
        prompt = render_audit_worker_prompt(
            self.store.user_text_for_session(session_id),
            plan.strategy, self.cfg.project.test_command or "(no test command)",
            **(extra_prompt or {}),
        )
        return {
            "exploration_id": ex_id,
            "strategy": plan.strategy,
            "prompt": prompt,
            # Provider-qualified for the opencode CLI; the driver writes the
            # box-local opencode.json from these fields.
            "model": (f"{self.cfg.models.provider}/"
                      f"{self.cfg.models.worker}"),
            # Never the real key: the box's egress policy adds it to model calls.
            "api_key": self.cfg.models.box_key_placeholder,
            "base_url": self.cfg.models.base_url,
            "tier": "flex",
            "harness": harness,
            "test_command": self.cfg.project.test_command,
            "baseline_commit": baseline_commit,
            "budget": {
                "max_wall_clock_s": self.cfg.explore.worker_wall_clock_minutes * 60,
                "max_agent_turns": self.cfg.explore.worker_max_agent_turns,
                # A redesign's tests can fail halfway, so tests don't stop it early.
                "early_stop_check_frac": 2.0,
                # Told to wrap up here, so it still lists its test changes and summary.
                "wrap_up_frac": 0.8,
            },
        }

    def _run(self, ex_id: str, plan: PlannedExploration,
             base_commit: str, harness: str,
             extra_prompt: dict[str, str] | None = None,
             start_patch: str | None = None) -> None:
        try:
            self.store.transition_exploration(ex_id, "PROVISIONING", "submitted")
            env = self.env.ensure_env(base_commit)
            box = self.sail.create_box(
                f"hs-{ex_id}", from_checkpoint_id=env.checkpoint_id,
                max_lifetime_seconds=int(self.cfg.explore.worker_wall_clock_minutes * 60 + 1800),
            )
            self.store.set_box_id(ex_id, box.id)

            # Repo: the env checkpoint already contains a /repo from its build
            # snapshot; overwrite with THIS exploration's base snapshot.
            with tempfile.NamedTemporaryFile(suffix=".tar.gz", delete=False) as tf:
                tarball = Path(tf.name)
            try:
                self.snapshotter.export_tarball(base_commit, tarball)
            except SecretsFound:
                self.store.transition_exploration(ex_id, "FAILED", "secret scan blocked upload")
                self.sail.terminate(box.id)
                return
            self.sail.upload_file(box.id, str(tarball), "/work/repo.tar.gz")
            tarball.unlink(missing_ok=True)

            # Git-local junk excludes BEFORE the baseline commit: the driver's
            # final `git add -A && git diff` must never emit binary .pyc diffs
            # (pytest regenerates them every run; `git apply --3way` cannot
            # merge binaries against the rebase box's own .pyc images, so the
            # WHOLE patch fails — 48/48 proposals
            # discarded "all rebases conflicted"). .git/info/exclude is
            # git-local: the user's repo and the patch stay untouched.
            baseline = self.sail.run(box.id, ["bash", "-c", (
                "rm -rf /repo && mkdir -p /repo /work && tar -xzf /work/repo.tar.gz -C /repo "
                "&& cd /repo && git init -q && "
                "printf '__pycache__/\\n*.pyc\\n.pytest_cache/\\n.venv/\\nvenv/\\nnode_modules/\\n' > .git/info/exclude && "
                "git add -A && "
                "git -c user.name=hindsight -c user.email=h@localhost commit -qm baseline "
                "&& git rev-parse HEAD"
            )], timeout=600)
            baseline_commit = baseline.stdout.strip().splitlines()[-1] if baseline.exit_code == 0 else ""
            if baseline.exit_code != 0:
                raise SailClientError(f"baseline setup failed: {baseline.stderr[-300:]}")
            if start_patch:
                self.sail.write_files(box.id, {"/work/start.diff": start_patch})
                applied = self.sail.run(box.id, ["bash", "-c", (
                    "cd /repo && git apply --whitespace=nowarn /work/start.diff")], timeout=600)
                if applied.exit_code != 0:
                    raise SailClientError(
                        f"the chosen design did not apply: {applied.stderr[-300:]}")

            ex_row = self.store.get_exploration(ex_id)
            task = self._task_json(
                ex_id, plan, harness, baseline_commit,
                session_id=ex_row["session_id"] if ex_row else "",
                extra_prompt=extra_prompt)
            driver_src = Path(__file__).parent / "box" / "driver.py"
            self.sail.write_files(box.id, {
                "/work/task.json": json.dumps(task),
                "/work/driver.py": driver_src.read_text(),
            })
            self.sail.start_background(box.id, ["bash", "-c", "cd /work && nohup python3 driver.py "
                                                 ">> /work/driver.log 2>&1 & echo started"])
            self.store.transition_exploration(ex_id, "RUNNING", "driver started")
            self._monitor_only(ex_id)
        except Exception as e:  # noqa: BLE001 - record, never crash the pool
            try:
                self.store.set_exit_reason(ex_id, f"error: {e}"[:500])
                self.store.transition_exploration(ex_id, "FAILED", str(e)[:200])
            except Exception:
                pass
        finally:
            # Whatever way this worker ended (proposal, failure, supersede),
            # a run-based worker notifies the selector, which fires when no
            # worker of the run is in flight.
            self._notify_selection(ex_id)

    def _run_followup(self, ex_id: str, messages: list[dict]) -> None:
        """Another turn for a finished worker, in its own conversation and box."""
        try:
            row = self.store.get_exploration(ex_id)
            box_id = row["box_id"]
            self.store.transition_exploration(ex_id, "PROVISIONING", "sent back")
            task = json.loads(self.sail.read_file(box_id, "/work/task.json"))
            task.update(
                exploration_id=ex_id,
                agent_session=agent_session_of(row),
                prompt=FOLLOWUP_PROMPT.format(
                    messages=render_user_messages([m["text"] for m in messages])))
            self.sail.run(box_id, ["bash", "-c",
                                   "rm -f /work/result.json /work/status.json"])
            self.sail.write_files(box_id, {"/work/task.json": json.dumps(task)})
            self.sail.start_background(box_id, ["bash", "-c", "cd /work && nohup python3 driver.py "
                                                ">> /work/driver.log 2>&1 & echo started"])
            self.store.transition_exploration(ex_id, "RUNNING", "driver started")
            self._monitor_only(ex_id)
        except Exception as e:  # noqa: BLE001 - record, never crash the pool
            try:
                self.store.set_exit_reason(ex_id, f"error: {e}"[:500])
                self.store.transition_exploration(ex_id, "FAILED", str(e)[:200])
            except Exception:
                pass
        finally:
            self._notify_selection(ex_id)

    def _notify_selection(self, ex_id: str) -> None:
        if self.selection is None:
            return
        try:
            row = self.store.get_exploration(ex_id)
            if row is not None and row["run_id"]:
                self.selection.on_exploration_finished(row["run_id"])
        except Exception:  # noqa: BLE001 - selection must never kill the worker thread
            log.exception("selection trigger failed for %s", ex_id)

    def _monitor_only(self, ex_id: str) -> None:
        """Poll /work/status.json until result.json appears, then finish up."""
        row = self.store.get_exploration(ex_id)
        if row is None or not row["box_id"]:
            self.store.transition_exploration(ex_id, "FAILED", "no box id")
            return
        box_id = row["box_id"]
        t0 = time.time()
        poll = self.cfg.explore.poll_interval_seconds
        deadline = t0 + self.cfg.explore.worker_wall_clock_minutes * 60 + 1800
        last_heartbeat = 0.0
        retried = False

        while time.time() < deadline:
            time.sleep(poll)
            try:
                state = self.store.exploration_state(ex_id)
                if state in ("SUPERSEDED", "CANCELLED"):
                    self.sail.terminate(box_id)
                    return
                # In this order: a message pushed to a worker that has already
                # finished would count as seen when it never was.
                if self.sail.file_exists(box_id, "/work/result.json"):
                    self._collect(ex_id, box_id)
                    return
                self._push_updates(ex_id, box_id)
                raw = self.sail.read_file(box_id, "/work/status.json")
                status = json.loads(raw)
                hb = status.get("heartbeat", 0.0)
                if hb > last_heartbeat:
                    last_heartbeat = hb
                elif time.time() - last_heartbeat > 120 and last_heartbeat > 0:
                    # stale status: retry once from the checkpoint
                    if not retried:
                        retried = True
                        self.sail.run(box_id, ["bash", "-c",
                                               "cd /work && nohup python3 driver.py "
                                               ">> /work/driver.log 2>&1 & echo restarted"])
                    else:
                        self.store.set_exit_reason(ex_id, "stale_status.json")
                        self.store.transition_exploration(ex_id, "FAILED", "stale status")
                        self.sail.terminate(box_id)
                        return
            except SailClientError:
                continue  # transient; next poll

        self.store.set_exit_reason(ex_id, "budget_time")
        self.store.transition_exploration(ex_id, "FAILED", "wall clock exceeded")
        self.sail.terminate(box_id)

    # Worker updates stream: in-flight workers get new USER
    # messages. Push-streaming into the agent's context is impossible (the
    # harness runs as one prompt session), so the orchestrator drops files
    # the worker is instructed to read before finalizing (worker_preamble
    # v2). Watermark per exploration; a missed poll is retried next cycle.
    def _push_updates(self, ex_id: str, box_id: str) -> None:
        if not self.cfg.council.stream_worker_updates:
            return
        row = self.store.get_exploration(ex_id)
        if row is None or not row["session_id"]:
            return
        key = f"updates_wm:{ex_id}"
        raw = self.store.kv_get(key)
        if raw is not None:
            after_id = int(raw)
        else:
            after_id = self.store.latest_user_message_id_at(
                row["session_id"], float(row["created_at"] or 0))
        msgs = self.store.user_messages_after(row["session_id"], after_id,
                                              limit=10)
        if not msgs:
            return
        # Files are keyed by the message ROW ID: timestamp-derived names can
        # collide (two messages within one millisecond), which would silently
        # overwrite an update the worker then never sees. Zero-padded ids
        # keep ls-order = chronological order.
        files = {f"/work/updates/u-{m['id']:06d}.json": json.dumps(
                     {"turn_ts": m["ts"], "text": m["text"]})
                 for m in msgs}
        try:
            self.sail.write_files(box_id, files)
        except SailClientError:
            return  # next poll retries from the same watermark
        self.store.kv_set(key, str(msgs[-1]["id"]))
        log.info("updates: pushed %d user message(s) to %s", len(files), ex_id)

    def _keep_transcript(self, ex_id: str, box_id: str) -> None:
        """The box will not outlive the run, and this is how to see what a worker
        did. Losing it never costs the worker its result."""
        try:
            raw = self.sail.read_file(box_id, "/work/events.jsonl")
            self.store.save_worker_transcript(
                ex_id, raw.decode(errors="replace")[-_MAX_TRANSCRIPT_CHARS:])
        except Exception as e:  # noqa: BLE001
            log.warning("no transcript from %s: %s", ex_id, str(e)[:120])

    def _bill_worker(self, ex_id: str, result: dict) -> None:
        """Workers call the model from their boxes, so nothing else records this."""
        from .budget import BudgetManager
        from .store import CostEntry
        model = self.cfg.models.worker
        tokens_in = int(result.get("input_tokens") or 0)
        tokens_out = int(result.get("output_tokens") or 0)
        self.store.record_cost(CostEntry(
            ref=ex_id, kind="model", model=model, tier="standard",
            input_tokens=tokens_in, output_tokens=tokens_out,
            usd=BudgetManager.token_cost_usd(model, tokens_in, tokens_out,
                                             self.cfg.models.prices)))

    def _collect(self, ex_id: str, box_id: str) -> None:
        result = json.loads(self.sail.read_file(box_id, "/work/result.json"))
        self._bill_worker(ex_id, result)
        self._keep_transcript(ex_id, box_id)
        # Honesty guard: a worker that errored (harness
        # missing/crashed, budget killed, no summary) must NEVER become a
        # proposal. Found live: with the un-installable `pi` harness every
        # worker exited 'error' with an empty patch (just the harness's own
        # untracked edge-test files), and the pipeline delivered those as
        # "completed redesigns" all night.
        # Out of time is different: the code is real, so it's kept and labelled.
        unfinished = result.get("exit_reason") == "budget_time"
        if unfinished and not result.get("summary"):
            result["summary"] = {"summary": UNFINISHED_SUMMARY}
        if result.get("exit_reason") != "done" and not unfinished:
            # Refused — the driver's own reason is preserved (a dead
            # harness, a budget kill, a missing summary block are all
            # visible in the store). The box is still cleaned up.
            self.store.set_exit_reason(
                ex_id, f"refused: {result.get('exit_reason')}"[:500])
            self.store.transition_exploration(
                ex_id, "FAILED",
                f"driver exit_reason={result.get('exit_reason')} — "
                "no proposal (honesty guard)")
            self.sail.terminate(box_id)
            return
        patch = self.sail.read_file(box_id, "/work/patch.diff").decode(errors="replace")
        preimages: dict[str, str] = {}
        try:
            raw = self.sail.read_file(box_id, "/work/preimages.tar.gz")
            with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as tar:
                for member in tar.getmembers():
                    if not member.isfile():
                        continue
                    data = tar.extractfile(member)
                    if data is None:
                        continue
                    # strip the leading "preimages/" arcname
                    rel = member.name.split("/", 1)[1] if "/" in member.name else member.name
                    preimages[rel] = base64.b64encode(data.read()).decode()
        except Exception as e:  # noqa: BLE001 - bridge absent/undecodable: --3way degrades to literal apply
            log.warning("no preimages from %s: %s", ex_id, str(e)[:120])
        self.store.set_exit_reason(ex_id, result.get("exit_reason", "done"))
        self.store.transition_exploration(ex_id, "TESTING", "driver finished")
        box_seconds = time.time() - (self.store.get_exploration(ex_id) or {"created_at": time.time()})["created_at"]
        from .store import CostEntry
        self.store.record_cost(CostEntry(ref=ex_id, kind="box", box_seconds=box_seconds,
                                          usd=0.002 * box_seconds / 60))

        proposal_id = self.store.save_proposal(
            ex_id, patch, result.get("summary") or {"summary": "(no summary)"},
            result.get("diff_stat", {}), preimages=preimages)
        self.store.transition_exploration(ex_id, "GATING", "proposal saved")

        # Integrity: plain code, before anything else looks at this design.
        row = self.store.get_exploration(ex_id)
        integrity = check_audit_integrity(
            patch, result.get("summary"),
            self.store.user_text_for_session(row["session_id"]))
        self.store.save_gate_result(
            proposal_id, "0_integrity", integrity.passed,
            {"findings": integrity.findings,
             "allowed_test_changes": integrity.allowed,
             "denied_test_changes": integrity.denied},
            ("ok" if integrity.passed else
             "test weakening / constraint violation")
            + (f"; {len(integrity.allowed)} test change(s) with a "
               "stated reason" if integrity.allowed else ""))
        tests = result.get("tests") or {}
        # Information for the critics, not a verdict.
        self.store.save_gate_result(
            proposal_id, "1_worker_tests", True,
            {"passed": tests.get("passed"), "failed": tests.get("failed")},
            "the worker's own final test run")
        if not integrity.passed:
            self.store.transition_exploration(ex_id, "DISCARDED", "gate stage 0 failed")
            self.sail.terminate(box_id)
            return

        # Passed: rest in GATING until selection decides. The winner's box must
        # survive for the main agent's questions, and which one wins is not known
        # yet, so each sleeps now and selection terminates the losers. The
        # checkpoint is the fallback if waking ever fails.
        try:
            cp = self.sail.checkpoint(
                box_id, ttl_seconds=_RESUME_CHECKPOINT_TTL,
                name=f"hs-resume-{ex_id}")
            self.store.set_exploration_checkpoint(ex_id, cp)
        except Exception:  # noqa: BLE001
            log.exception("resume checkpoint failed for %s", ex_id)
        try:
            self.sail.sleep(box_id)
        except SailClientError:
            log.warning("could not put %s to sleep; terminating it", box_id)
            self.sail.terminate(box_id)
