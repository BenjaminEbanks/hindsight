"""Session gate: is this session worth acting on?

All checks are computed directly from stored events. Every decision is logged
with its reasons (for observability).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .config import HindsightConfig
from .design_review import Verdict
from .store import Store


@dataclass
class GateResult:
    session_id: str
    worthy: bool
    priority: str  # "low" | "raised"
    checks: dict = field(default_factory=dict)  # name -> {"passed": bool, "detail": str}
    reasons: list[str] = field(default_factory=list)


def preconditions(store: Store, cfg: HindsightConfig,
                  session_id: str) -> tuple[bool, dict[str, dict], list[str]]:
    """Checked before a review is paid for, and again once it comes back."""
    checks: dict[str, dict] = {}
    reasons: list[str] = []

    # --- Required: code can be run ------------------------------------------
    # Non-empty latest usable snapshot; a project need not have tests. Actual
    # runnability is proven at env build (M-B+); this check is the cheap
    # precondition, and the detail says so honestly.
    snap = store.latest_usable_snapshot(session_id)
    files = (snap["snapshot_files"] or 0) if snap is not None else 0
    code_ok = snap is not None and files > 0
    checks["code_can_run"] = {
        "passed": code_ok,
        "detail": (f"usable snapshot with {files} files; runnability proven at "
                   f"env build" if code_ok else "no usable non-empty snapshot")}
    if not code_ok:
        reasons.append("code not demonstrably present/runnable")

    # --- Required: no background run already ongoing for this session --------
    # New messages are streamed to that run instead. Checked before any
    # detection is paid for, and detection resumes once it is sent or dropped.
    busy = (cfg.council.one_run_at_a_time
            and store.session_has_active_run(session_id))
    checks["no_active_run"] = {
        "passed": not busy,
        "detail": "a run is ongoing for this session" if busy else "none"}
    if busy:
        reasons.append("a background run is ongoing for this session")

    # --- Required: a result could still be delivered -------------------------

    worthy = code_ok and not busy

    return worthy, checks, reasons


def evaluate_gate(store: Store, cfg: HindsightConfig, session_id: str,
                  verdict: Optional[Verdict] = None) -> GateResult:
    """Record the decision. No verdict means no review happened or it failed."""
    worthy, checks, reasons = preconditions(store, cfg, session_id)
    yes = verdict is not None and verdict.yes
    if yes:
        reasons.append(f"design review: {verdict.reason}")
    checks["design_review"] = {
        "passed": yes,
        "detail": verdict.reason if verdict is not None else "no verdict"}
    priority = "raised" if yes else "low"

    result = GateResult(
        session_id=session_id, worthy=worthy, priority=priority,
        checks=checks, reasons=reasons,
    )
    # Observability: every gate decision is logged with its
    # reasons, so behavior can be inspected and evaluated.
    latest = store.latest_turn(session_id)
    store.save_session_check(
        session_id, latest["id"] if latest else None, worthy, priority,
        checks, reasons)
    return result
