"""Eval metrics.

Everything is computed from the recorded episode/fork outcomes — no
re-derivation, no invention:

  - final tests passed / failed and user turns, per arm;
  - helped-rate: forks where the accept branch passed the hidden tests
    and the ignore branch did not;
  - staleness rate: runs that went stale before delivery over all
    completed runs;
  - cost per useful nudge: recorded spend over helpful nudges;

The zero-nudge control is checked by the caller (task.no_change) against
nudge_count == 0; compute_metrics reports the numbers it needs.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .episode import EpisodeResult
from .fork import ForkOutcome
from .tasks import Requirement, Task



# Every arm that runs the Hindsight pipeline. Hardcoding "treatment" here
# silently dropped the relay and diff arms from these metrics.
TREATMENT_ARMS = ("treatment", "treatment_relay", "treatment_diff")

_STALE_RUN_STATES = ("DROPPED_STALE",)
_COMPLETED_RUN_STATES = ("DELIVERED", "DROPPED_STALE", "NONE_QUALIFIED",
                         "ALL_FAILED")


@dataclass
class EvalMetrics:
    tasks: int = 0
    episodes: int = 0
    # per-arm final hidden-test outcomes
    arm_final_ok: dict[str, list[bool]] = field(default_factory=dict)
    turns_by_arm: dict[str, list[int]] = field(default_factory=dict)
    # nudges / runs
    nudges_total: int = 0
    no_change_tasks_clean: int = 0      # control tasks with zero nudges
    no_change_tasks: int = 0
    completed_runs: int = 0
    stale_runs: int = 0
    # forks
    forks: int = 0
    helped: int = 0
    hurt: int = 0                        # accept strictly worse than ignore
    # cost
    spend_usd: float = 0.0

    @property
    def helped_rate(self) -> Optional[float]:
        return round(self.helped / self.forks, 4) if self.forks else None

    @property
    def staleness_rate(self) -> Optional[float]:
        return (round(self.stale_runs / self.completed_runs, 4)
                if self.completed_runs else None)

    @property
    def cost_per_useful_nudge(self) -> Optional[float]:
        return (round(self.spend_usd / self.helped, 4)
                if self.helped else None)

    def arm_pass_rate(self, arm: str) -> Optional[float]:
        outcomes = self.arm_final_ok.get(arm, [])
        return (round(sum(outcomes) / len(outcomes), 4)
                if outcomes else None)

    def to_dict(self) -> dict:
        return {
            "tasks": self.tasks, "episodes": self.episodes,
            "arm_pass_rate": {a: self.arm_pass_rate(a)
                              for a in self.arm_final_ok},
            "turns_by_arm": {a: v for a, v in self.turns_by_arm.items()},
            "nudges_total": self.nudges_total,
            "no_change_control": (
                f"{self.no_change_tasks_clean}/{self.no_change_tasks} "
                "zero-nudge"),
            "completed_runs": self.completed_runs,
            "stale_runs": self.stale_runs,
            "staleness_rate": self.staleness_rate,
            "forks": self.forks, "helped": self.helped, "hurt": self.hurt,
            "helped_rate": self.helped_rate,
            "spend_usd": round(self.spend_usd, 4),
            "cost_per_useful_nudge": self.cost_per_useful_nudge,
        }


def compute_metrics(
    tasks: list[Task],
    results: list[EpisodeResult],
    forks: list[ForkOutcome],
) -> EvalMetrics:
    m = EvalMetrics(tasks=len(tasks), episodes=len(results))
    by_task = {t.id: t for t in tasks}
    for res in results:
        ok = bool(res.final and res.final.ok)
        m.arm_final_ok.setdefault(res.arm, []).append(ok)
        m.turns_by_arm.setdefault(res.arm, []).append(res.turns)
        m.nudges_total += res.nudge_count
        m.spend_usd += res.spend_usd
        if res.arm in TREATMENT_ARMS:
            for run in res.runs:
                if run["state"] in _COMPLETED_RUN_STATES:
                    m.completed_runs += 1
                    if run["state"] in _STALE_RUN_STATES:
                        m.stale_runs += 1
            task = by_task.get(res.task_id)
            if task is not None and task.no_change:
                m.no_change_tasks += 1
                if res.nudge_count == 0 and not res.runs:
                    m.no_change_tasks_clean += 1
    for f in forks:
        m.forks += 1
        if f.helped:
            m.helped += 1
        elif f.delta_passed < 0:
            m.hurt += 1
    return m
