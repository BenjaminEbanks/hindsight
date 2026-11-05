"""SessionSource abstraction and normalized events.

The orchestrator consumes normalized events from a SessionSource, so other
harnesses can be added later without touching the rest of the system.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterator, Literal, Protocol

from pydantic import BaseModel, Field

EventKind = Literal[
    "user_message",
    "turn_completed",
    "message_removed",
    "session_compacted",
    "session_error",
    "files_edited",
    "session_ended",
]


class SessionEvent(BaseModel):
    kind: EventKind
    session_id: str
    turn_id: str | None = None  # = user message ID
    message_ids: list[str] = Field(default_factory=list)
    messages: list[dict] = Field(default_factory=list)  # raw harness messages (delta)
    paths: list[str] = Field(default_factory=list)  # for files_edited
    ts: float = Field(default_factory=float, description="unix seconds; 0 = set by receiver")


class SessionSource(Protocol):
    """A source of normalized session events (OpenCode plugin, replay, Claude Code, ...)."""

    def events(self) -> Iterator[SessionEvent]: ...


@dataclass
class TurnDecision:
    """What the orchestrator should do in response to an ingested event.

    Mirrors the plugin event table :
      - user_message    -> pre-turn snapshot; record manual edits since last post-turn snapshot
      - turn_completed  -> store messages; post-turn snapshot; detection
      - message_removed -> mark turn reverted (strong correction signal)
      - session_error   -> turn marked errored; snapshot still taken
    """

    action: Literal[
        "pre_turn", "process_turn", "revert_turn", "error_turn", "churn", "log", "duplicate"
    ]
    event: SessionEvent


def _normalize_ws(s: str) -> str:
    return " ".join(s.split())


class TurnAssembler:
    """Deduplicates turn_completed events and turns them into decisions.

    Idle can fire more than once for the same turn (after abort, retry, or
    compaction). The orchestrator deduplicates on
    (session ID, last user message ID, last assistant message ID).
    """

    def __init__(self, seen_turns: set[str] | None = None):
        self.seen: set[str] = seen_turns or set()

    def _dedup_key(self, event: SessionEvent) -> str:
        ids = event.message_ids[-2:] if event.message_ids else []
        return event.session_id + "|" + "|".join(ids)

    def ingest(self, event: SessionEvent) -> TurnDecision:
        if event.kind == "user_message":
            return TurnDecision("pre_turn", event)
        if event.kind == "turn_completed":
            key = self._dedup_key(event)
            if key in self.seen:
                return TurnDecision("duplicate", event)
            self.seen.add(key)
            return TurnDecision("process_turn", event)
        if event.kind == "message_removed":
            return TurnDecision("revert_turn", event)
        if event.kind == "session_error":
            return TurnDecision("error_turn", event)
        if event.kind == "files_edited":
            return TurnDecision("churn", event)
        if event.kind == "session_ended":
            return TurnDecision("session_end", event)
        return TurnDecision("log", event)  # session_compacted and anything else
