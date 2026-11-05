"""Should background engineers be called in? One model call per turn, reading
the user's words and the code as it now stands.

The question is the product's: knowing everything the user has said, would this
have been built differently? Plain bugs stay with the main agent.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Callable, Optional

from .config import HindsightConfig
from .prompts import load, render
from .requirement_audit import render_user_messages
from .snapshot import Snapshotter
from .store import Store

log = logging.getLogger(__name__)


@dataclass
class Verdict:
    yes: bool
    reason: str


def run_reason_key(run_id: str) -> str:
    return f"run_reason:{run_id}"


def run_dismissed_key(run_id: str) -> str:
    return f"run_dismissed:{run_id}"


def render_code(snapshotter: Snapshotter, store: Store, session_id: str,
                commit: str, max_chars: int,
                keep: Callable[[str], bool] = lambda path: True) -> str:
    """The code at this snapshot, what the agent just touched first, so that
    the limit cuts the files least likely to matter. Files the secret scan flags
    never go to a model."""
    flagged = {f.path.split(":", 1)[-1] for f in snapshotter.secret_findings(commit)}
    paths = [p for p in snapshotter.files_at(commit) if p not in flagged and keep(p)]
    recent = set(store.files_changed_recent(session_id, limit=3))
    paths.sort(key=lambda p: (p not in recent, p))
    parts: list[str] = []
    omitted: list[str] = []
    used = 0
    for path in paths:
        text = snapshotter.file_at(commit, path)
        if "\0" in text[:4096]:
            continue
        if used + len(text) > max_chars:
            omitted.append(path)
            continue
        used += len(text)
        parts.append(f"--- {path} ---\n{text}")
    if omitted:
        parts.append("(Not shown, too large to include: " + ", ".join(omitted) + ")")
    return "\n\n".join(parts) or "(no files)"


def _render_past(store: Store, session_id: str) -> str:
    lines = []
    for run in store.runs_for_session(session_id):
        reason = store.kv_get(run_reason_key(run["id"]))
        if not reason:
            continue
        dismissed = store.kv_get(run_dismissed_key(run["id"]))
        outcome = ("offered to the user, who turned it down" if dismissed else
                   "offered to the user" if run["state"] == "DELIVERED" else
                   "no redesign came of it")
        lines.append(f"- {reason} ({outcome})")
    return "\n".join(lines) or "(nothing yet)"


def build_prompt(store: Store, snapshotter: Snapshotter, cfg: HindsightConfig,
                 session_id: str) -> Optional[str]:
    """None when there is nothing to review: the first request is the task
    itself, not a change to it."""
    messages = store.user_text_for_session(session_id)
    snap = store.latest_usable_snapshot(session_id)
    if len(messages) < 2 or snap is None:
        return None
    return render(
        load("design_review"),
        past=_render_past(store, session_id),
        earlier="\n\n".join(messages[:-1]),
        latest=messages[-1],
        code=render_code(snapshotter, store, session_id, snap["post_snapshot"],
                          cfg.experiments.review_max_code_chars))


def parse_verdict(raw: str) -> Verdict:
    """Raises ValueError on anything but a clear verdict."""
    data = json.loads(raw[raw.find("{"):raw.rfind("}") + 1])
    if not isinstance(data.get("call_background"), bool):
        raise ValueError("no boolean verdict")
    return Verdict(data["call_background"],
                   str(data.get("what_no_longer_holds") or "")[:500])


MAX_DIRECTIONS = 5


def build_directions_prompt(store: Store, snapshotter: Snapshotter, cfg: HindsightConfig,
                            session_id: str, base_commit: str, reason: str) -> str:
    return render(
        load("design_directions"),
        user_messages=render_user_messages(store.user_text_for_session(session_id)),
        reason=reason,
        code=render_code(snapshotter, store, session_id, base_commit,
                          cfg.experiments.review_max_code_chars))


def parse_directions(raw: str) -> list[str]:
    """Raises ValueError if the answer isn't JSON with a list of directions."""
    data = json.loads(raw[raw.find("{"):raw.rfind("}") + 1])
    if not isinstance(data.get("directions"), list):
        raise ValueError("no list of directions")
    found: list[str] = []
    for item in data["directions"]:
        structure = str(item.get("structure") or "").strip() if isinstance(item, dict) else ""
        if not structure:
            continue
        text = f"{str(item.get('name') or '').strip()}: {structure}"[:1500]
        if why := str(item.get("why") or "").strip():
            text += f"\nServes: {why[:500]}"
        if text not in found:
            found.append(text)
    return found[:MAX_DIRECTIONS]


def plan_directions(prompt: str, model_fn: Callable[[str], str]) -> list[str]:
    """With no directions, the open worker still runs."""
    try:
        return parse_directions(model_fn(prompt))
    except Exception:  # noqa: BLE001 - planning must never break a session
        log.exception("choosing directions failed")
        return []


def review(prompt: str, model_fn: Callable[[str], str]) -> Optional[Verdict]:
    """A failed review starts nothing. The next turn reviews every request again,
    so nothing is lost for good."""
    try:
        return parse_verdict(model_fn(prompt))
    except Exception:  # noqa: BLE001 - detection must never break a session
        log.exception("design review failed")
        return None
