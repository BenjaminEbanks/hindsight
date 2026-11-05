"""Eval episode runner.

One episode = one staged task + one main-agent harness + (for treatment
arms) the full Hindsight pipeline driven through the same SessionEvent
surface production uses. Everything that happens in an episode is the real
machinery: design review, gate, edge-case engine, redesign runs,
selection, delivery. Nothing is simulated except the human.

The episode serializes stage boundaries (wait for background quiet after
each turn) so measurements are deterministic: nudges arrive at a known
point, and the fork-at-nudge logic (M-F(c)) can branch there.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from ..config import HindsightConfig
from ..delivery import render_agent_message
from ..sources.base import SessionEvent
from ..trace import build_timeline, render_timeline
from .scoring import HiddenTestResult, run_hidden_tests
from .tasks import Task

log = logging.getLogger("hindsight")

_ACTIVE_RUN_STATES = ("NEW", "PLANNED", "LAUNCHED", "SELECTING")
# Explorations the *system* still owes work on. NUDGE_READY (awaiting
# delivery timing) and SHOWN (awaiting the user) are resolved by a delivery
# decision, not by more background work, so they do not keep an episode
# unquiet — otherwise the episode would wait for the simulated user forever.
_EXPLORATION_WORK_STATES = ("PENDING", "PROVISIONING", "RUNNING", "TESTING",
                            "GATING")

SIMULATED_CONSENT = ("Thanks. Go ahead and apply what you recommend, whether that is "
                     "all of it, part of it or only its ideas. If you recommend none "
                     "of it, leave the code as it is.")

# Settings that make an episode fast enough to measure; every one of them
# is a spec-Section-8 experiment knob, legitimately overridable per
# episode (spec: "all overridable per eval episode").
DEFAULT_OVERRIDES: dict[str, float] = {
    "settle_seconds": 0.5,
    "quiet_poll_seconds": 1.0,
    "flush_timeout": 60.0,
}


@dataclass
class EpisodeResult:
    task_id: str
    arm: str
    turns: int
    nudges: list[dict] = field(default_factory=list)
    runs: list[dict] = field(default_factory=list)
    final: Optional[HiddenTestResult] = None
    spend_usd: float = 0.0
    quiet_error: str = ""

    @property
    def nudge_count(self) -> int:
        return len(self.nudges)


class EvalEpisode:
    def __init__(
        self,
        task: Task,
        repo_dir: str | Path,
        *,
        main_agent,
        hindsight: bool = True,
        hindsight_home: str | Path | None = None,
        sail=None,
        model_fn=None,
        judge_model_fn=None,
        settings_overrides: dict | None = None,
        models_override: dict[str, str] | None = None,
        relay_nudges: bool = False,
        relay_diff: bool = False,
        session_id: str = "eval",
        arm: str = "treatment",
        quiet_timeout: float = 2400.0,
        extra_stages: list | None = None,
        on_nudge=None,
        configure: Callable[[HindsightConfig], None] | None = None,
        reviewer_model_fn=None,
        max_diff_chars: int | None = None,
        agent_has_tools: bool = True,
    ):
        # Runs last on the episode's config; benchmarks set their project up here.
        self.configure = configure
        self.reviewer_model_fn = reviewer_model_fn
        # The agent here has no hindsight_show, so a cut-off diff cannot be finished.
        self.max_diff_kwargs = {"max_diff_chars": max_diff_chars} if max_diff_chars else {}
        self.max_diff_kwargs["has_tools"] = agent_has_tools
        self.task = task
        self.repo_dir = Path(repo_dir)
        self.main_agent = main_agent
        self.hindsight = hindsight
        self.hindsight_home = (Path(hindsight_home) if hindsight_home
                               else self.repo_dir.parent / "hs")
        self.sail = sail
        self.model_fn = model_fn
        self.judge_model_fn = judge_model_fn
        self.overrides = {**DEFAULT_OVERRIDES, **(settings_overrides or {})}
        # Weak-model experiments: field-name -> model id for
        # hindsight/ModelsConfig (worker, reviewer, judge, ...).
        # Applied in _build_orchestrator; unknown fields raise loudly
        # instead of being silently ignored.
        self.models_override = dict(models_override or {})
        # Relay arm: the simulated user passes a newly shown
        # nudge to the main agent with their NEXT stage message (found
        # In the plain treatment arm the nudge text is only
        # "shown" to the simulated user, who never relays it — so
        # treatment == baseline + waiting, and no information effect is
        # measurable without adoption). False keeps the historical
        # treatment-arm semantics.
        self.relay_nudges = relay_nudges
        # Diff delivery: the redesign's FULL
        # DIFF goes to the main agent in-chat (agent-mediated delivery)
        # — the agent audits it and decides whether to incorporate. The
        # user gets a pointer notification, and can ask the agent about
        # the change. Text-only relay (relay_nudges) stays available for
        # the "what should a nudge carry?" comparison.
        self.relay_diff = relay_diff
        if relay_diff and not relay_nudges:
            raise ValueError(
                "relay_diff requires relay_nudges (the diff rides the "
                "same relay queue — never a silent no-op)")
        if arm == "full_context" and self.hindsight:
            raise ValueError(
                "the full_context arm measures the main agent with "
                "everything stated up front — hindsight must be OFF")
        self.holds: list[dict] = []
        self._relay_queue: list[dict] = []        # Episode-level knobs may arrive in settings_overrides too; pull
        # them out so they are not silently no-oped by the setattr guard.
        # (The override only applies when the explicit parameter was left
        # at its default — found the old code assigned from
        # settings_overrides first and then clobbered it with the default
        # parameter, so the override never took effect.)
        if "quiet_timeout" in self.overrides and quiet_timeout == 2400.0:
            quiet_timeout = float(self.overrides.pop("quiet_timeout"))
        self.session_id = session_id
        self.arm = arm
        self.quiet_timeout = quiet_timeout
        # Baseline B appends a final redesign stage; fork arms resume the
        # remaining stages. Callers pass the full stage list to run.
        self.extra_stages = extra_stages or []
        # on_nudge(nudge: dict, stage_index: int) fires at a stage boundary
        # when a nudge was newly SHOWN during that stage's quiet wait
        # (fork-at-nudge snapshots here). The orchestrator is exposed as
        # .orch while run() is in flight for exactly this purpose.
        self.on_nudge = on_nudge
        self._seen_nudge_ids: set[str] = set()
        self.orch = None

    # -------------------------------------------------------------- public

    def run(self, stages: list | None = None) -> EpisodeResult:
        """Run the (remaining) stages of the task through the episode."""
        stages = (stages if stages is not None else self.task.stages) \
            + list(self.extra_stages)
        # Arm 2: everything stated up front,
        # one turn, hindsight off — the information ceiling against
        # which streamed (baseline) and streamed+background
        # (treatment/treatment_diff) are read.
        if self.arm == "full_context":
            return self._run_full_context(self._brief_stages(stages), stages)
        orch =self._build_orchestrator() if self.hindsight else None
        self.orch = orch
        try:
            for i, stage in enumerate(stages):
                # Production event order: the user
                # message arrives BEFORE the agent works, so the pre-turn
                # snapshot captures the repo as the agent found it. Doing
                # it after main_agent.send() made every turn's pre and
                # post snapshots identical (turn_files always empty, the
                # ledger's changed_files always "(none)") — which is why
                # no live probe ever recorded design assumptions, fired
                # the gate, or delivered a nudge .
                # Relay arm: nudges shown during the
                # PREVIOUS stage's quiet wait ride along with this
                # message — the simulated user mentions them, as a real
                # user would ("while you were working I got this note...").
                if stage.closing:
                    # Everything delivered has to arrive before the wrap-up,
                    # including when stages did not wait for the background.
                    self._wait_background(orch, label="before closing")
                    self._fire_nudge_hook(orch, i)
                    self._hand_over_designs()
                message = stage.message
                if stage.closing:
                    # Not observed by Hindsight: anything built from this
                    # turn could never reach the agent.
                    self.main_agent.send(message)
                    continue
                self._emit_user_message(orch, i, message)
                reply = self.main_agent.send(message)
                self._emit_turn_completed(orch, i, message, reply)
                # Waiting for the background gives every result a free pass
                # to arrive first. A gap sends the next request on a clock,
                # as a real user would, so runs and messages can overlap.
                gap = float(self.overrides.get("stage_gap_seconds") or 0)
                if gap and orch is not None:
                    orch.flush(timeout=self.overrides["flush_timeout"])
                    time.sleep(gap)
                    orch.flush(timeout=self.overrides["flush_timeout"])
                    orch.delivery.maybe_deliver(self.session_id)
                    if self.overrides.get("wait_for_run"):
                        self._hold_for_run(orch, i)
                else:
                    self._wait_background(orch, label=f"stage {i + 1}")
                self._fire_nudge_hook(orch, i)
                self._hand_over_designs()
            # Anything still running gets to arrive before scoring.
            if orch is not None:
                self._wait_background(orch, label="final")
                self._fire_nudge_hook(orch, len(stages))
                self._hand_over_designs()
            return self._score(orch, turns=len(stages))
        finally:
            self.orch = None
            if orch is not None:
                orch.shutdown()

    # ------------------------------------------------------------ internals

    @staticmethod
    def _brief_stages(stages) -> list:
        return [s for s in stages if not s.closing]

    def _run_full_context(self, stages, all_stages=None) -> EpisodeResult:
        """Arm 2: the user states every request up front in
        one brief — the same information the streamed arm receives, but
        nothing arrives late. One design turn (plus the closing turn every
        arm gets, if any); the agent designs once with complete knowledge;
        hindsight stays off. Graded identically (final repo vs hidden
        tests)."""
        closing = [s for s in (all_stages or []) if s.closing]
        brief = (
            "We're planning this properly — here is EVERYTHING we will "
            "need, stated up front, in the order it would have come up. "
            "Please build the complete solution for all of it now:\n\n"
            + "\n\n".join(f"({i + 1}) {s.message}"
                          for i, s in enumerate(stages)))
        self._emit_user_message(None, 0, brief)
        reply = self.main_agent.send(brief)
        self._emit_turn_completed(None, 0, brief, reply)
        for stage in closing:
            self.main_agent.send(stage.message)
        return self._score(None, turns=1 + len(closing))

    def _hand_over_designs(self) -> None:
        """The plugin's job in a real session: at a turn boundary, a ready design
        becomes its own agent turn. The agent reports and waits for the user, so
        the simulated user answers. Hindsight does not observe these turns."""
        if not self.relay_nudges:
            self._relay_queue = []
            return
        items, self._relay_queue = self._relay_queue, []
        latest = self.orch.store.latest_turn(self.session_id) if items else None
        for nudge in items:
            # Long patches are stored as a reference; the agent needs the text.
            patch = (self.orch.store._blob_get(nudge.get("patch_blob") or "")
                     if self.relay_diff else "")
            limit = self.max_diff_kwargs.get("max_diff_chars")
            share = getattr(self.main_agent, "share_file", None)
            path = (share(f"design-{nudge.get('id')}.diff", patch)
                    if share and limit and len(patch) > limit else None)
            self.main_agent.send(render_agent_message(
                nudge.get("text") or "",
                patch,
                nudge.get("basis_turn_n"),
                json.loads(nudge.get("missed_json") or "[]"),
                latest_turn_n=latest["n"] if latest is not None else None,
                description=self.orch.store.design_description(nudge.get("proposal_id")),
                full_diff_path=path,
                **self.max_diff_kwargs))
            self.main_agent.send(SIMULATED_CONSENT)

    def _fire_nudge_hook(self, orch, stage_index: int) -> None:
        """Call on_nudge for every nudge newly shown during this stage's
        quiet wait (delivery happens inside _wait_background)."""
        if orch is None:
            return
        if self.on_nudge is None and not self.relay_nudges:
            return
        # The proposal's patch + facts ride along for the diff arm
        # (agent-mediated delivery); harmless for text relay.
        rows = orch.store._query(
            "SELECT n.id, n.proposal_id, n.text, n.shown_at, n.channel, n.missed_json, "
            "p.patch_blob, p.summary_json, e.base_turn_id "
            "FROM nudges n JOIN proposals p ON n.proposal_id=p.id "
            "JOIN explorations e ON p.exploration_id=e.id "
            "WHERE e.session_id=? AND n.shown_at IS NOT NULL "
            "ORDER BY n.shown_at", (self.session_id,))
        basis = {}
        for row in rows:
            t = orch.store.get_turn(row["base_turn_id"] or "")
            if t is not None:
                basis[row["base_turn_id"]] = t["n"]
        for row in rows:
            if row["id"] in self._seen_nudge_ids:
                continue
            self._seen_nudge_ids.add(row["id"])
            item = dict(row)
            item["basis_turn_n"] = basis.get(row["base_turn_id"])
            self._relay_queue.append(item)
            if self.on_nudge is not None:
                self.on_nudge(dict(row), stage_index)

    def _build_orchestrator(self):
        from ..orchestrator import Orchestrator

        cfg = HindsightConfig(project_dir=self.repo_dir,
                              hindsight_home=self.hindsight_home)
        cfg.project.test_command = self.task.test_command
        for key, value in self.models_override.items():
            if not hasattr(cfg.models, key):
                raise ValueError(
                    f"models_override: unknown ModelsConfig field {key!r} "
                    f"(valid: {', '.join(type(cfg.models).model_fields)})")
            setattr(cfg.models, key, value)
        if "worker_poll_seconds" in self.overrides:
            cfg.explore.poll_interval_seconds = self.overrides["worker_poll_seconds"]
        for key, value in self.overrides.items():
            try:
                setattr(cfg.experiments, key, value)
            except (ValueError, AttributeError):
                pass  # not an experiments knob (e.g. episode-only setting)
        if self.configure is not None:
            self.configure(cfg)
        kwargs = {}
        if self.model_fn is not None:
            kwargs["model_fn"] = self.model_fn
        if self.reviewer_model_fn is not None:
            kwargs["reviewer_model_fn"] = self.reviewer_model_fn
        if self.judge_model_fn is not None:
            kwargs["judge_model_fn"] = self.judge_model_fn
        if self.sail is not None:
            kwargs["sail"] = self.sail
        return Orchestrator(cfg, **kwargs)

    def _emit_user_message(self, orch, n: int, user_text: str) -> None:
        """Emit the user_message event (pre-turn snapshot) BEFORE the agent
        works — production order. See run()."""
        if orch is None:
            return
        orch.handle_event(SessionEvent(
            kind="user_message", session_id=self.session_id, turn_id=f"m{n}",
            ts=time.time()))
        orch.flush()

    def _emit_turn_completed(self, orch, n: int, user_text: str,
                             reply: str) -> None:
        """Emit turn_completed (post-turn snapshot) after the agent works."""
        mid, aid = f"m{n}", f"a{n}"
        if orch is None:
            return
        orch.handle_event(SessionEvent(
            kind="turn_completed", session_id=self.session_id, turn_id=mid,
            message_ids=[mid, aid],
            messages=[{"id": mid, "role": "user", "text": user_text},
                      {"id": aid, "role": "assistant", "text": reply}],
            ts=time.time()))
        orch.flush()

    def _hold_for_run(self, orch, stage_index: int) -> None:
        """Holds the next request until a running redesign ends, to test whether a design
        helps when it arrives in time. Each hold is recorded, because a real user wouldn't
        wait: a hold over a run is a design that would have landed late."""
        run_active = bool(orch.store.session_has_active_run(self.session_id))
        if not run_active and self._quiet(orch):
            return
        started = time.time()
        self._wait_background(orch, label=f"hold after stage {stage_index + 1}")
        self.holds.append({"after_stage": stage_index + 1, "run_active": run_active,
                           "next_request_was_due": started,
                           "held_seconds": round(time.time() - started)})

    def _wait_background(self, orch, label: str = "") -> None:
        """Block until the background machinery has nothing in flight:
        settle + idle have had their window, no live explorations, no runs
        in active states."""
        if orch is None:
            return
        # Let the settle and idle timers fire first (their windows are the
        # overridden, short ones).
        poll = self.overrides["quiet_poll_seconds"]
        time.sleep(self.overrides["settle_seconds"] + poll)
        deadline = time.time() + self.quiet_timeout
        last_log = 0.0
        while time.time() < deadline:
            # An undrained event queue is not quiet: the turn handler can
            # be blocked behind a long callback (e.g. an edge search under
            # the guard), and the latest RECORDED turn looking complete
            # says nothing about events still in flight (found live
            # The fork episode's final turn was never recorded
            # because its handler queued behind a stuck edge search).
            if not orch.flush(timeout=self.overrides["flush_timeout"]):
                time.sleep(poll)
                continue
            if self._quiet(orch):
                return
            # The plugin's question at a gap between turns; the agent is
            # between turns whenever this episode is waiting.
            if self._delivery_pending(orch):
                orch.delivery.maybe_deliver(self.session_id)
            if time.time() - last_log > 60:
                last_log = time.time()
                log.info("episode %s: waiting for background (%s)",
                         label, self._active_summary(orch))
            time.sleep(poll)
        log.warning("episode %s: quiet timeout after %.0fs", label,
                    self.quiet_timeout)

    def _quiet(self, orch) -> bool:
        if orch.reviews_open():
            return False
        active = orch.store._query(
            f"SELECT 1 FROM runs WHERE session_id=? AND state IN "
            f"({','.join('?' for _ in _ACTIVE_RUN_STATES)}) LIMIT 1",
            (self.session_id, *_ACTIVE_RUN_STATES))
        if active:
            return False
        marks = ",".join("?" for _ in _EXPLORATION_WORK_STATES)
        working = orch.store._query(
            f"SELECT 1 FROM explorations e LEFT JOIN runs r ON e.run_id = r.id "
            f"WHERE e.session_id=? AND e.state IN ({marks}) AND "
            f"(r.id IS NULL OR r.state IN "
            f"({','.join('?' for _ in _ACTIVE_RUN_STATES)})) LIMIT 1",
            # A worker left over from a finished run cannot change the result.
            (self.session_id, *_EXPLORATION_WORK_STATES, *_ACTIVE_RUN_STATES))
        if working:
            return False
        if self._delivery_pending(orch):
            return False
        # The latest turn's background chain must also be complete: the
        # settle-guard verdict and the idle-gate decision.
        # Without these the episode can reach shutdown while the turn
        # handler is still mid-turn (the handler holds the
        # callback guard across the model call) — shutdown then sets
        # _stopping and the settle/idle timers are silently dropped, so
        # the FINAL turn never gets a gate decision and a stage-3 design
        # break can never nudge (seen live: two trials ended
        # with usable=None and 2 session checks for 3 turns).
        turn = orch.store.latest_turn(self.session_id)
        if turn is not None and turn["usable"] is None:
            return False
        if turn is not None:
            check = orch.store.latest_session_check(self.session_id)
            if check is None or check["turn_id"] != turn["id"]:
                return False
        return True

    def _delivery_pending(self, orch) -> bool:
        """A READY_TO_DELIVER run whose winner proposal has no recorded
        delivery decision yet (shown OR held — a held decision is final for
        this episode; the hold condition is real, not a hang)."""
        row = orch.store._query(
            "SELECT 1 FROM runs WHERE session_id=? AND state='READY_TO_DELIVER' "
            "AND winner_proposal_id IS NOT NULL AND NOT EXISTS ("
            "SELECT 1 FROM deliveries d "
            "WHERE d.proposal_id=runs.winner_proposal_id) LIMIT 1",
            (self.session_id,))
        return bool(row)

    def _active_summary(self, orch) -> str:
        rows = orch.store._query(
            "SELECT state, COUNT(*) AS n FROM runs WHERE session_id=? "
            "GROUP BY state", (self.session_id,))
        runs = ", ".join(f"{r['state']}x{r['n']}" for r in rows) or "no runs"
        return runs

    def _score(self, orch, turns: int) -> EpisodeResult:
        result = EpisodeResult(task_id=self.task.id, arm=self.arm, turns=turns)
        result.final = run_hidden_tests(self.task, self.repo_dir)
        if orch is not None:
            store = orch.store
            result.nudges = [
                {"proposal_id": n["proposal_id"], "text": n["text"],
                 "shown_at": n["shown_at"]}
                for n in store._query(
                    "SELECT n.* FROM nudges n JOIN proposals p "
                    "ON n.proposal_id=p.id JOIN explorations e "
                    "ON p.exploration_id=e.id WHERE e.session_id=? "
                    "AND n.shown_at IS NOT NULL", (self.session_id,))]
            (self.hindsight_home / "trace.txt").write_text(
                render_timeline(build_timeline(store, self.session_id)) + "\n")
            result.runs = [
                {"id": r["id"], "state": r["state"],
                 "exit_reason": r["exit_reason"],
                 "winner": r["winner_proposal_id"]}
                for r in store._query(
                    "SELECT * FROM runs WHERE session_id=?", (self.session_id,))]
            spend = store._query(
                "SELECT SUM(usd) AS s FROM costs")
            result.spend_usd = round(spend[0]["s"] or 0.0, 4) if spend else 0.0
        return result
