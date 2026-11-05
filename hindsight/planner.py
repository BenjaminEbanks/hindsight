"""What one worker is asked to do."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class PlannedExploration:
    strategy: str
    base_turn_id: str
    base_turn_n: int
    target_ids: list[str] = field(default_factory=list)
    # NB: prompt-time extras travel via Executor.submit(extra_prompt=...),
    # not on this dataclass .
