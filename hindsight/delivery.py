"""Delivery and dismissal memory
.

Timing: at the first gap between turns after a design is ready. The plugin asks
then, and the check for unseen messages runs at that moment, because messages are
stored when their turn ends. The one-run-per-session limit paces deliveries.

Recipient: the main agent, which reviews the design and tells the user what it
recommends. It applies nothing until the user agrees. The text is rendered by the
cheap model from CHECKED FACTS stored in the run/proposal (executed test outcomes,
diff stat, worker assumptions). The engineers' own description travels with it, labelled
unverified. The desktop toast shows the same text to the user.

Dismissal memory: the design review is told what the user turned down.

A design that has fallen behind is still sent, saying how many messages behind it
is, so the agent can use its ideas. late_messages="retry_final" sends it back to the
worker first instead.
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Callable, Optional, Sequence, TYPE_CHECKING

if TYPE_CHECKING:
    from .resume import WorkerResume
    from .selection import Selector

from .config import HindsightConfig
from .design_review import run_dismissed_key, run_reason_key
from .models import call_context
from .prompts import load, render
from .store import Store

log = logging.getLogger("hindsight")


def dismissal_fingerprint(target_requirement_ids: list[str], strategy: str) -> dict:
    return {
        "requirements": sorted(target_requirement_ids),
        "strategy": strategy,
    }


def _summary_assumptions(proposal: dict) -> list[str]:
    summary = proposal.get("summary")
    if not isinstance(summary, dict):
        return []
    return [a.get("assumption", str(a)) if isinstance(a, dict) else str(a)
            for a in summary.get("assumptions") or []]


DELIVERY_MARKER = "[Hindsight delivery]"
_MAX_DIFF_CHARS = 24000
_MAX_MISSED_CHARS = 400
_MAX_FAILURES = 10
_MAX_FAILURE_CHARS = 200


def render_agent_message(facts: str, patch: str, basis_turn_n=None,
                         missed: Sequence[str] = (),
                         max_diff_chars: int = _MAX_DIFF_CHARS,
                         has_tools: bool = True,
                         latest_turn_n: int | None = None,
                         description: str = "",
                         proposal_id: str | None = None,
                         full_diff_path: str | None = None) -> str:
    parts = [
        f"{DELIVERY_MARKER} This came from Hindsight, the background system, not "
        "from the user. It finished a design and is handing it to you.\n",
        facts.strip(),
        "\nThis is a suggestion, not a patch you have to apply. You can use all of it, "
        "part of it, or only its ideas, written your own way on your current code. "
        "It is a proposal about structure, so weigh what it makes easier to change as "
        "well as how the code behaves: a redesign can be worth using when behavior stays "
        "the same, and it can also not be worth its cost.",
    ]
    if has_tools and proposal_id:
        parts.append(f"\nThis design's id is {proposal_id}, for hindsight_show, "
                     "hindsight_ask and hindsight_adopt.")
    if description:
        parts.append("\nTHE ENGINEERS' OWN DESCRIPTION (not verified; check it against "
                     f"the diff):\n{description}")
    patch = patch.strip()
    if patch:
        basis = (f"your code after conversation message #{basis_turn_n}"
                 if basis_turn_n else "an earlier version of your code")
        if len(patch) > max_diff_chars:
            if full_diff_path:
                rest = f"; the whole diff is in {full_diff_path}"
            elif has_tools and proposal_id:
                rest = "; hindsight_show has the rest"
            else:
                rest = ""
            patch = patch[:max_diff_chars] + f"\n... (diff truncated{rest})"
        behind = (latest_turn_n - basis_turn_n
                  if basis_turn_n and latest_turn_n else 0)
        if behind > 0:
            parts.append(
                f"\nThis design is {behind} message{'s' if behind != 1 else ''} behind: it "
                f"was built on your code after message #{basis_turn_n}, and the conversation "
                f"is now at message #{latest_turn_n}. It was tested on that older code, not "
                "your current code, and may not apply as written. Its ideas may still be "
                "useful: rebuild what you want on your current code.")
        parts.append(f"\nTHE DIFF, built against {basis}. Check it against the "
                     f"current code yourself:\n```diff\n{patch}\n```")
    parts.append(
        "\nReview it, then tell the user in a few sentences what it does and "
        "what you recommend: all of it, part of it, only its ideas, or none, saying "
        "which and why. Do not change any code until the user agrees. Then, to apply "
        "all of it as written, use hindsight_adopt if you have it; otherwise make the "
        "edits yourself. You can put questions to the engineer who built it with "
        "hindsight_ask." if has_tools else
        "\nReview it, then tell the user in a few sentences what it does and "
        "what you recommend: all of it, part of it, only its ideas, or none, saying "
        "which and why. Do not change any code until the user agrees. Then make the "
        "edits yourself. There are no Hindsight tools here, and this message and any "
        "file it names are everything Hindsight has for you.")
    if missed:
        listed = "\n".join(f"- {m[:_MAX_MISSED_CHARS]}" for m in missed)
        parts.insert(3, "\nThis design was built before these messages from the "
                        "user. Check it against them and the current code before "
                        f"you recommend it:\n{listed}")
    return "\n".join(parts)


class DeliveryManager:
    def __init__(self, cfg: HindsightConfig, store: Store,
                 nudge_fn: Callable[[str], str]):
        self.cfg = cfg
        self.store = store
        self.nudge_fn = nudge_fn
        # Wired by the orchestrator after construction.
        self.selector: "Selector | None" = None
        self.resume: "WorkerResume | None" = None

    # -------------------------------------------------------------- public

    def maybe_deliver(self, session_id: str) -> list[dict]:
        """Attempt delivery for every READY_TO_DELIVER run of the session.
        Returns the decision log. Never raises."""
        decisions: list[dict] = []
        try:
            for run in self.store.runs_in_state(session_id, "READY_TO_DELIVER"):
                decisions.append(self._deliver_run(run, session_id))
        except Exception:  # noqa: BLE001 - delivery must never crash the caller
            log.exception("delivery attempt failed for %s", session_id)
        return decisions

    def agent_message(self, nudge: dict) -> str:
        """What the main agent receives. Marked, so that Hindsight never takes
        its own delivery for a user request or a permission to adopt."""
        return render_agent_message(nudge["text"], nudge.get("patch") or "",
                                    nudge.get("basis_turn_n"),
                                    nudge.get("missed") or (),
                                    latest_turn_n=nudge.get("latest_turn_n"),
                                    description=nudge.get("description") or "",
                                    proposal_id=nudge.get("proposal_id"))

    def record_dismissal(self, proposal_id: str, reason: str) -> None:
        """The user dismissed a delivered proposal; remember the fingerprint
        so the same direction is never re-delivered in this session."""
        p = self.store.get_proposal(proposal_id)
        if p is None:
            return
        ex = self.store.get_exploration(p["exploration_id"])
        if ex is None:
            return
        run = self.store.get_run(ex["run_id"]) if ex["run_id"] else None
        if run is None:
            return
        fp = self._fingerprint(run, ex["strategy"])
        self.store.save_dismissal(ex["session_id"], fp, reason)
        # The next design review sees this, so it does not ask for the same again.
        self.store.kv_set(run_dismissed_key(run["id"]), reason or "dismissed")
        log.info("dismissal recorded: %s (%s)", reason, fp["requirements"])

    # ------------------------------------------------------------ internals

    def _deliver_run(self, run, session_id: str) -> dict:
        proposal = (self.store.get_proposal(run["winner_proposal_id"])
                    if run["winner_proposal_id"] else None)
        if proposal is None:
            self._drop(run, "winner proposal missing")
            return {"run": run["id"], "outcome": "dropped",
                    "reason": "winner proposal missing"}

        ex = self.store.get_exploration(proposal["exploration_id"])

        # --- is a turn still going? Its message is not stored yet ------------
        latest = self.store.latest_turn(session_id)
        if latest is not None and latest["status"] == "in_progress":
            return {"run": run["id"], "outcome": "held", "reason": "turn_in_progress"}

        # --- has the user said anything it has not seen? ---------------------
        unseen = self.store.messages_unseen_by(proposal["exploration_id"])
        if unseen and self.cfg.experiments.late_messages == "retry_final":
            self.store.save_delivery(proposal["id"], shown=False,
                                     held_reason="newer_user_message")
            if self.selector is None:
                return {"run": run["id"], "outcome": "held",
                        "reason": "newer_user_message"}
            self.selector.send_back(run, proposal, unseen)
            return {"run": run["id"], "outcome": "sent_back",
                    "reason": "newer_user_message"}

        # --- deliver ---------------------------------------------------------
        text = self._render_nudge(run, proposal)
        nudge_id = f"n-{uuid.uuid4().hex[:8]}"
        self.store.save_nudge(nudge_id, proposal["id"], "user", text)
        if unseen:
            self.store.set_nudge_missed(nudge_id, [m["text"] for m in unseen])
        # The plugin toast pushes this text; the row is the record.
        self.store.mark_nudge_shown(nudge_id, "plugin_toast")
        self.store.save_delivery(proposal["id"], shown=True,
                                 channel="plugin_toast")
        if ex is not None:
            self.store.transition_exploration(ex["id"], "SHOWN", "delivered")
        self.store.transition_run(
            run["id"], "DELIVERED", f"nudge {nudge_id} released"
            + (f"; it has not seen {len(unseen)} newer message(s)" if unseen else ""))
        if self.resume is not None:
            self.resume.keep(proposal["exploration_id"])
        log.info("delivery: run %s proposal %s released", run["id"],
                 proposal["id"])
        return {"run": run["id"], "outcome": "delivered",
                "proposal": proposal["id"]}

    def _drop(self, run, reason: str) -> None:
        if self.selector is not None:
            self.selector.end_run(run["id"], "DROPPED_STALE", reason)
        else:
            self.store.transition_run(run["id"], "DROPPED_STALE", reason)

    def _fingerprint(self, run, strategy: str) -> dict:
        return dismissal_fingerprint(
            json.loads(run["target_requirement_ids_json"] or "[]"), strategy)

    def _render_nudge(self, run, proposal: dict) -> str:
        """Nothing shows a design is better, and it may have failed its check. The
        evidence line is built from stored results and appended outside the model's
        reach, so the reader always sees it."""
        gate = self.store.gate_features(proposal["id"], "2_audit_gate")
        worker_tests = gate.get("worker_tests_unverified") or {}
        passed = len(worker_tests.get("passed") or [])
        not_passed = len(worker_tests.get("not_passed") or [])
        fixed = gate.get("existing_failures_fixed") or []
        failures = gate.get("regressions") or []
        rewritten = gate.get("tests_rewritten") or []
        stat = proposal.get("diff_stat") or {}
        basis = self._basis(proposal)
        evidence = (
            f"Evidence: this is a redesign made from your messages, built "
            f"against {basis}. No failing test prompted it. "
            f"{int(gate.get('existing_tests_still_passing', 0))} existing "
            "test(s) still pass with this change"
            + (", with no regressions." if not failures else "."))
        if fixed:
            evidence += (f" {len(fixed)} existing test(s) that fail on your "
                         f"code now pass: {', '.join(fixed)}.")
        evidence += (f" The worker's {passed + not_passed} new test(s) "
                     f"({not_passed} not passing) are its own and unverified.")
        if gate.get("no_project_suite"):
            evidence = (
                f"Evidence: this is a redesign made from your messages, built "
                f"against {basis}. No failing test prompted it. "
                "There is no project test suite to check it against: it was "
                "only checked by running the worker's own build and tests "
                f"(`{gate.get('verify_command')}`), which it wrote itself.")
        if failures:
            shown = "; ".join(str(f)[:_MAX_FAILURE_CHARS] for f in failures[:_MAX_FAILURES])
            more = (f"; and {len(failures) - _MAX_FAILURES} more"
                    if len(failures) > _MAX_FAILURES else "")
            evidence += (" CHECK THESE: it failed its check on the code it was built on, "
                         "even after a retry. Each may be a real bug, or a test tied to "
                         f"the old structure: {shown}{more}. hindsight_adopt will refuse this design, "
                         "so make any edits you want yourself.")
        if rewritten:
            listed = "; ".join(f"{c.get('test')} ({c.get('reason')})" for c in rewritten)
            evidence += f" CHECK THESE: it changed existing test(s): {listed}."
        assumptions = _summary_assumptions(proposal)
        prompt = render(
            load("delivery_nudge_audit"),
            reason=self.store.kv_get(run_reason_key(run["id"])) or "(not recorded)",
            basis=basis,
            added=int(stat.get("added", 0)),
            removed=int(stat.get("removed", 0)),
            files=int(stat.get("files", 0)),
            assumptions="; ".join(assumptions) or "(none)",
        )
        try:
            with call_context(run_id=run["id"]):
                text = (self.nudge_fn(prompt) or "").strip()
        except Exception:  # noqa: BLE001 - degrade to the raw facts, never fake
            log.exception("nudge render failed; using raw facts")
            text = ""
        if not text:
            text = (f"An alternative version is ready: "
                    f"+{int(stat.get('added', 0))}/-"
                    f"{int(stat.get('removed', 0))} across "
                    f"{int(stat.get('files', 0))} file(s).")
        return f"{text[:max(0, 1998 - len(evidence))]}\n{evidence}"[:2000]

    def _basis(self, proposal: dict) -> str:
        # Basis framing:
        # the agent VERIFIES nudges against its repo — a correct-but-stale
        # nudge with no stated basis gets rejected whole. Name the turn the
        # workers built against so the reader can reconcile instead.
        ex = self.store.get_exploration(proposal.get("exploration_id", ""))
        if ex is not None and ex["base_turn_id"]:
            t = self.store.get_turn(ex["base_turn_id"])
            if t is not None:
                return f"your code after conversation message #{t['n']}"
        return "(basis unavailable)"
