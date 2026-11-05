"""The orchestrator: wires all components and implements
the per-turn and per-idle flow.

Everything runs locally except Sail work; all state lives in the SQLite
event store, so the process can crash and resume.

Event handling is asynchronous: `handle_event` ingests, queues, and returns
immediately (the plugin's send budget is 200ms; the pipeline may take
seconds). A single worker thread processes events in order. Turn completion
runs the settle guard, then detection straight away.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
import queue
import threading
import time
from typing import Optional

from .budget import BudgetManager
from .config import HindsightConfig
from .council import Council
from .adopt import AdoptManager
from .models import call_context, make_chat_client, make_model_fn, recorded
from . import design_review
from .delivery import DELIVERY_MARKER, DeliveryManager
from .env import EnvManager
from .executor import Executor
from .reaper import Reaper
from .runs import RunManager
from .resume import WorkerResume
from .sail_client import SailClient
from .selection import Selector
from .session_gate import evaluate_gate, preconditions
from .snapshot import SnapshotError, Snapshotter
from .sources.base import SessionEvent, TurnAssembler, TurnDecision
from .store import Store

log = logging.getLogger("hindsight")


class Orchestrator:
    def __init__(self, cfg: HindsightConfig, *, model_fn=None,
                 judge_model_fn=None, reviewer_model_fn=None,
                 sail: SailClient | None = None):
        self.cfg = cfg
        cfg.hindsight_dir.mkdir(parents=True, exist_ok=True)
        self.store = Store(cfg.db_path)
        self.snapshotter = Snapshotter(
            cfg.project_dir, cfg.snapshot_repo, cfg.all_snapshot_excludes(),
            cfg.project.max_file_mb)
        self.snapshotter.ensure()
        self.sail = sail or SailClient(cfg.app_name, box_policy=cfg.models.box_policy)
        self.chat = make_chat_client(cfg.models, self.sail)
        self.budget = BudgetManager(self.store, cfg)
        self.env = EnvManager(cfg, self.store, self.sail, self.snapshotter)
        self._model_fn = model_fn or self._default_model_fn()
        self._reviewer_model_fn = reviewer_model_fn or model_fn or make_model_fn(
            self.chat, cfg.models.reviewer, self.budget, ref_prefix="reviewer",
            max_output_tokens=16000)
        # Not the reviewer override: a forced review still needs real directions.
        self._directions_model_fn = model_fn or make_model_fn(
            self.chat, cfg.models.reviewer, self.budget, ref_prefix="reviewer",
            max_output_tokens=16000)
        # A review can take a minute or more. It must not hold up event handling,
        # or the agent could edit before its pre-turn snapshot is taken.
        self._reviews = ThreadPoolExecutor(max_workers=1,
                                           thread_name_prefix="design-review")
        self._reviews_lock = threading.Lock()
        self._reviews_open = 0
        self.executor = Executor(
            cfg, self.store, self.snapshotter, self.sail, self.env)
        self.assembler = TurnAssembler()
        self.runs = RunManager(cfg, self.store, self.budget, self.executor)
        # Critics and chairman: cross-family vs the workers (Kimi-K3 in real
        # use); injected model fns are reused in tests.
        self._judge_model_fn = judge_model_fn or (
            model_fn if model_fn is not None else self._default_judge_model_fn())
        self.selector = Selector(
            cfg, self.store, self.sail, self.env, self.snapshotter,
            self.executor, self.budget)
        self.executor.selection = self.selector
        self.selector.council = Council(
            cfg, self.store,
            recorded(self.store, cfg.models.judge, "council", self._judge_model_fn),
            model_name=cfg.models.judge)
        self.selector.runs = self.runs
        # hindsight_ask's deeper questions go to the run's sleeping winner.
        self.resume = WorkerResume(cfg, self.store, self.sail)
        self.reaper = Reaper(self.sail, self.store, on_tick=self.resume.expire)
        # The nudge writer renders from stored facts; the plugin hands them to
        # the main agent, and the toast shows them to the user.
        self.delivery = DeliveryManager(cfg, self.store, recorded(
            self.store, cfg.models.nudge, "delivery_nudge_audit", self._model_fn))
        self.delivery.selector = self.selector
        self.delivery.resume = self.resume
        # Adoption engine: the only component allowed to
        # write the user's repo, on explicit command only.
        self.adopt = AdoptManager(cfg, self.store,
                                  on_adopted=self.resume.release_proposal)
        self.reaper.start()
        self.executor.resume()
        self.selector.resume()

        # Async event pipeline: one ordered worker thread.
        self._work: queue.Queue = queue.Queue()
        self._processing = False
        self._idle = threading.Condition()
        self._worker = threading.Thread(
            target=self._process_loop, name="hindsight-orchestrator", daemon=True)
        self._worker.start()
        # Per-session timers. Settle checks (short, ~3s) always run — a new
        # event must not cancel the previous turn's usability marking. Idle
        # gate timers are canceled by any new activity (the session is no
        # longer quiet).
        self._settle_timers: dict[str, threading.Timer] = {}  # turn_id -> timer
        self._timers_lock = threading.Lock()
        # Shutdown handshake (a straggler idle timer
        # fired after shutdown() closed the store -> "Cannot operate on a
        # closed database"). Timer.cancel() only prevents timers that have
        # not started; callbacks already running are waited for via the
        # guard, and no new callback work starts once _stopping is set.
        self._stopping = threading.Event()
        self._callback_guard = threading.RLock()

    # ------------------------------------------------------------ model fn

    def _default_model_fn(self):
        return make_model_fn(self.chat, self.cfg.models.nudge, self.budget,
                                  ref_prefix="nudge")

    def _default_judge_model_fn(self):
        # Time follows how much the model writes, not the prompt: GLM-5.3-Flash wrote
        # 34k-55k tokens per critique at about 30 tokens a second, 19-25 minutes.
        return make_model_fn(self.chat, self.cfg.models.judge, self.budget,
                                  ref_prefix="judge", timeout_seconds=1800.0, retries=1)

    # -------------------------------------------------------- event intake

    def handle_event(self, event: SessionEvent) -> TurnDecision:
        if not event.ts:
            event = event.model_copy(update={"ts": time.time()})
        decision = self.assembler.ingest(event)
        self.store.upsert_session(event.session_id, self.cfg.project_id,
                                  "opencode", "unknown")
        self._work.put((event, decision))
        return decision

    def flush(self, timeout: float = 60.0) -> bool:
        """Wait until every queued event has been processed (tests, shutdown)."""
        deadline = time.time() + timeout
        with self._idle:
            while (self._work.qsize() or self._processing) and time.time() < deadline:
                self._idle.wait(max(0.01, deadline - time.time()))
            return not self._work.qsize() and not self._processing

    def _process_loop(self) -> None:
        while True:
            item = self._work.get()
            if item is None:
                with self._idle:
                    self._idle.notify_all()
                return
            event, decision = item
            with self._idle:
                self._processing = True
            try:
                handler = {
                    "pre_turn": self._on_pre_turn,
                    "process_turn": self._on_turn_completed,
                    "revert_turn": self._on_revert,
                    "error_turn": self._on_error,
                    "churn": self._on_churn,
                    "session_end": self._on_session_end,
                    "log": lambda e: None,
                    "duplicate": lambda e: None,
                }[decision.action]
                self._guarded(handler, (event,))
            except Exception:
                log.exception("event handler failed for %s", event.kind)
            finally:
                with self._idle:
                    self._processing = False
                    self._idle.notify_all()

    # ------------------------------------------------------------- timers

    def _guarded(self, fn, args=()):
        """Run a timer callback unless shutdown is in progress, holding the
        callback guard so shutdown() cannot close the store under a
        callback that is already mid-flight. Returns what fn returns, or None if
        shutdown came first."""
        if self._stopping.is_set():
            return None
        with self._callback_guard:
            if self._stopping.is_set():
                return
            return fn(*args)

    def _schedule_settle(self, turn_id: str, delay: float, fn, args=()) -> None:
        t = threading.Timer(delay, self._guarded, args=(fn, args))
        t.daemon = True
        with self._timers_lock:
            self._settle_timers[turn_id] = t
        t.start()

    # -------------------------------------------------------- turn flow

    def _next_turn_n(self, session_id: str) -> int:
        row = self.store.latest_turn(session_id)
        return (row["n"] + 1) if row else 1

    def _on_pre_turn(self, event: SessionEvent) -> None:
        """Pre-turn snapshot; record manual edits since the last post-turn
        snapshot."""
        session_id = event.session_id
        n = self._next_turn_n(session_id)
        prev = self.store.latest_turn(session_id)
        try:
            snap = self.snapshotter.snapshot(session_id, n, "pre")
            pre_commit: Optional[str] = snap.commit
        except SnapshotError:
            # Still create the turn row (pre=None): without it, a failed
            # pre-snapshot makes the NEXT turn's events attach to the
            # previous turn's row (seen live). The turn will
            # simply have no pre/post diff.
            log.exception("pre-turn snapshot failed for turn %d", n)
            pre_commit = None
        turn_id = self.store.start_turn(session_id, n, event.turn_id or "", pre_commit)
        if pre_commit is not None and prev is not None and prev["post_snapshot"]:
            edits = self.snapshotter.manual_edits_between(prev["post_snapshot"], pre_commit)
            if edits:
                self.store.add_manual_edits(turn_id, edits)

    def _on_turn_completed(self, event: SessionEvent) -> None:
        session_id = event.session_id
        row = self.store.latest_turn(session_id)
        if row is None:
            self._on_pre_turn(event)
            row = self.store.latest_turn(session_id)
        assert row is not None
        n = row["n"]
        turn_id = row["id"]
        # Hindsight's own deliveries are not things the user asked for.
        self.store.add_messages(session_id, turn_id, [
            m for m in event.messages
            if not (m.get("role") == "user"
                    and str(m.get("text") or "").startswith(DELIVERY_MARKER))])
        try:
            snap = self.snapshotter.snapshot(session_id, n, "post")
        except SnapshotError:
            # Mark the turn errored rather than leaving it in_progress:
            # a stuck row makes the next turn's events attach to it, and
            # detection would run against a pre-turn state anyway
            # (seen live: three dead handlers, turn 2 stuck,
            # turn 3 never recorded).
            log.exception("post-turn snapshot failed for turn %d", n)
            self.store.complete_turn(turn_id, "snapshot_error", None)
            # Errored turns are unusable by definition; the verdict must be
            # recorded so a waiter on the settle chain (eval episodes) is
            # not left hanging on usable=NULL.
            self.store.mark_turn_usable(turn_id, False,
                                        "post-turn snapshot failed")
            return
        self.store.complete_turn(turn_id, "ok", snap.commit, snapshot_files=snap.files)
        changed = self.snapshotter.files_changed_since(row["pre_snapshot"])
        self.store.record_turn_files(turn_id, changed)

        # Settle guard: schedule the usability check.
        fingerprint = self.snapshotter.work_tree_fingerprint()
        self._schedule_settle(
            turn_id, self.cfg.experiments.settle_seconds, self._settle_check,
            args=(session_id, turn_id, n, fingerprint))

    def _settle_check(self, session_id: str, turn_id: str, n: int,
                      fingerprint_at_turn: str) -> None:
        """Mark the turn's snapshot usable once files have settled,
        then run detection.

        Detection runs even when the snapshot is unusable, so a decision is
        always recorded. Skipping it on an unusable snapshot left the session with no gate decision at all,
        so eval episodes waiting on one would hang until timeout (found
        while fixing the dropped-final-turn chain)."""
        exp = self.cfg.experiments
        busy = self.snapshotter.git_busy()
        if busy is not None:
            self.store.mark_turn_usable(turn_id, False, f"git {busy} in progress")
            log.info("turn %d snapshot unusable: git %s in progress", n, busy)
        elif self.snapshotter.work_tree_fingerprint() != fingerprint_at_turn:
            time.sleep(exp.settle_seconds)  # one retry
            if self.snapshotter.work_tree_fingerprint() != fingerprint_at_turn:
                self.store.mark_turn_usable(turn_id, False, "files still changing")
                log.info("turn %d snapshot unusable: files still changing", n)
            else:
                self.store.mark_turn_usable(turn_id, True, "settled")
        else:
            self.store.mark_turn_usable(turn_id, True, "settled")
        self._detect(session_id)

    def designs_for_agent(self, session_id: str) -> list[dict]:
        """The plugin asks this when the agent is between turns. Messages are
        stored when their turn ends, so this is the one moment the check for
        unseen messages can be trusted: release happens here, not when the
        design was finished."""
        with self._idle:
            if self._work.qsize() or self._processing:
                return []
        self._guarded(self.delivery.maybe_deliver, (session_id,))
        return [{"id": d["id"], "text": self.delivery.agent_message(d)}
                for d in self.store.pending_agent_deliveries(session_id)]

    def _detect(self, session_id: str) -> None:
        """Would this have been built differently? Ask once the cheap checks
        pass, and start a run on a yes."""
        worthy, _, _ = preconditions(self.store, self.cfg, session_id)
        prompt = (design_review.build_prompt(
            self.store, self.snapshotter, self.cfg, session_id) if worthy else None)
        if prompt is None:
            self._decide(session_id, None)
            return
        with self._reviews_lock:
            self._reviews_open += 1
        try:
            self._reviews.submit(self._review, session_id, prompt)
        except RuntimeError:  # shutting down, and this timer was already running
            with self._reviews_lock:
                self._reviews_open -= 1
            log.info("review skipped for %s: shutting down", session_id[:12])

    def reviews_open(self) -> int:
        """Reviews queued or running. A slow review's verdict is recorded against
        the latest turn, so finished checks cannot show whether one is still out."""
        with self._reviews_lock:
            return self._reviews_open

    def _review(self, session_id: str, prompt: str) -> None:
        try:
            with call_context(session_id=session_id):
                verdict = design_review.review(prompt, recorded(
                    self.store, self.cfg.models.reviewer, "design_review",
                    self._reviewer_model_fn))
            self._decide(session_id, verdict)
        finally:
            with self._reviews_lock:
                self._reviews_open -= 1

    def _decide(self, session_id: str, verdict) -> None:
        result = self._guarded(evaluate_gate, (self.store, self.cfg, session_id, verdict))
        if result is None:
            return
        log.info("gate[%s]: worthy=%s priority=%s reasons=%s",
                 session_id[:12], result.worthy, result.priority, result.reasons)
        if not (result.worthy and result.priority == "raised"):
            return
        run_id = self._start_run(session_id, [self._turn_label(session_id)],
                                 reason=verdict.reason)
        if run_id is not None:
            self._guarded(self.store.kv_set,
                          (design_review.run_reason_key(run_id), verdict.reason))

    def _turn_label(self, session_id: str) -> str:
        turn = self.store.latest_turn(session_id)
        return f"T{turn['n']}" if turn is not None else "T?"

    def _start_run(self, session_id: str, targets: list[str],
                   strategies: list[str] | None = None,
                   reason: str = "The user asked for this by hand.") -> Optional[str]:
        turn = self.store.latest_turn(session_id)
        base = self.store.latest_usable_snapshot(session_id)
        base_commit = (base["post_snapshot"] if base is not None
                       else (turn["post_snapshot"] if turn else None))
        if base_commit is None:
            log.info("run skipped: no usable snapshot")
            return None
        if self.cfg.council.one_run_at_a_time and \
                self.store.session_has_active_run(session_id):
            log.info("run deferred: this session has a run")
            return None
        # A slow model call, so outside the guard: holding it would stop the agent's
        # next pre-turn snapshot from being taken.
        directions = self._choose_directions(session_id, base_commit, reason)

        def launch() -> Optional[str]:
            # Another review can finish while directions are chosen.
            if self.cfg.council.one_run_at_a_time and \
                    self.store.session_has_active_run(session_id):
                log.info("run deferred: this session has a run")
                return None
            return self.runs.start_audit_run(
                session_id, targets, base_commit,
                turn["id"] if turn else "", turn["n"] if turn else 0,
                session_start=self._session_start_ts(session_id),
                strategies=strategies, directions=directions)

        return self._guarded(launch)

    def _choose_directions(self, session_id: str, base_commit: str, reason: str) -> list[str]:
        prompt = design_review.build_directions_prompt(
            self.store, self.snapshotter, self.cfg, session_id, base_commit, reason)
        with call_context(session_id=session_id):
            return design_review.plan_directions(prompt, recorded(
                self.store, self.cfg.models.reviewer, "design_directions",
                self._directions_model_fn))

    def _on_revert(self, event: SessionEvent) -> None:
        row = self.store.latest_turn(event.session_id)
        if row is not None:
            self.store.complete_turn(row["id"], "reverted", row["post_snapshot"])
            self.store.mark_turn_usable(row["id"], False, "turn reverted")

    def _on_error(self, event: SessionEvent) -> None:
        """Turn marked errored; snapshot still taken, unusable."""
        row = self.store.latest_turn(event.session_id)
        if row is not None:
            snap = self.snapshotter.snapshot(event.session_id, row["n"], "post")
            self.store.complete_turn(row["id"], "errored", snap.commit)
            self.store.mark_turn_usable(row["id"], False, "turn errored")

    def _on_churn(self, event: SessionEvent) -> None:
        # files_edited between turns feeds churn tracking via turn_files at
        # turn completion; nothing extra to do here.
        return

    def _on_session_end(self, event: SessionEvent) -> None:
        self.resume.release_session(event.session_id)

    def _session_start_ts(self, session_id: str) -> Optional[float]:
        row = self.store._query_one(
            "SELECT started_at FROM sessions WHERE id=?", (session_id,))
        return row["started_at"] if row else None

    # ---------------------------------------------------------- explorations

    def explore_now(self, session_id: str, strategies: list[str] | None = None) -> list[str]:
        """Pull mode: skip the gate, not the budget or the one-run rule."""
        run_id = self._start_run(session_id, ["manual"], strategies)
        return [e["id"] for e in self.store.run_explorations(run_id)] if run_id else []

    # ------------------------------------------------------------- status

    def ask(self, proposal_id: str, question: str | None = None) -> dict:
        """`hindsight_ask`: read-only proposal facts (summary, assumptions, tests,
        diff) and, for a deeper question, the winning worker's own answer. Never
        performs an action."""
        p = self.store.get_proposal(proposal_id)
        if p is None:
            return {"error": "unknown proposal"}
        ex = self.store.get_exploration(p["exploration_id"])
        bundle = {
            "proposal_id": p["id"],
            "exploration_id": p["exploration_id"],
            "state": ex["state"] if ex is not None else None,
            "strategy": ex["strategy"] if ex is not None else None,
            "summary": p.get("summary"),
            "assumptions": (p.get("summary") or {}).get("assumptions", [])
            if isinstance(p.get("summary"), dict) else [],
            "diff_stat": p.get("diff_stat", {}),
            "requirement_flips": p.get("requirement_flips") or [],
            "regressions": p.get("regressions") or [],
            "qualified": p.get("qualified"),
            "diff": p.get("patch"),
            "resumable": self.resume.can_resume(proposal_id),
        }
        if question is None or not question.strip():
            return bundle
        answer = self.resume.answer(proposal_id, question)
        return {**bundle, "question": question, "answer": answer}

    def status(self) -> dict:
        live = [dict(r) for r in self.store.live_explorations()]
        day_spend = self.store.spend_since(time.time() - 24 * 3600)
        return {
            "state": "exploring" if live else "idle",
            "explorations": [{k: r[k] for k in ("id", "strategy", "state", "box_id")}
                             for r in live],
            "spend_24h_usd": round(day_spend, 4),
            "budget_per_day_usd": self.cfg.budget.per_day_usd,
        }

    def shutdown(self) -> None:
        """Stop background threads and close the store. A timer callback
        already in flight (cancel() only stops timers that have not
        started) is waited for, bounded, so a straggler callback can never
        touch a closed database . If the wait expires the
        store connection is left open rather than crashed under."""
        self._stopping.set()
        self._reviews.shutdown(wait=False, cancel_futures=True)
        self.reaper.stop()
        self._work.put(None)
        self._worker.join(timeout=10)
        with self._timers_lock:
            timers = list(self._settle_timers.values())
            self._settle_timers.clear()
        for t in timers:
            t.cancel()
        if self._callback_guard.acquire(timeout=30):
            try:
                self.store.close()
            finally:
                self._callback_guard.release()
        else:
            log.warning("shutdown: timer callback still running after 30s; "
                        "leaving the store connection open (unused from now)")
