"""Run manager: one run = one worker per direction, one open worker, one selection and
at most one delivery. The run's state machine lives in the store (`runs` / `run_events`);
this module owns planning and the budget check.
"""

from __future__ import annotations

import json
import logging
from typing import Optional

from .budget import BudgetManager
from .config import HindsightConfig
from .executor import Executor
from .planner import PlannedExploration
from .store import Store

log = logging.getLogger("hindsight")

OPEN_WORKER = "open_redesign"


class RunManager:
    def __init__(
        self,
        cfg: HindsightConfig,
        store: Store,
        budget: BudgetManager,
        executor: Executor,
    ):
        self.cfg = cfg
        self.store = store
        self.budget = budget
        self.executor = executor

    # -------------------------------------------------------------- public

    def start_audit_run(
        self,
        session_id: str,
        target_requirement_ids: list[str],
        base_commit: str,
        turn_id: str,
        turn_n: int,
        *,
        session_start: Optional[float] = None,
        strategies: Optional[list[str]] = None,
        directions: Optional[list[str]] = None,
    ) -> Optional[str]:
        """One worker per direction, plus one with none."""
        if not target_requirement_ids:
            return None
        run_id = self.store.create_run(session_id, 0,
                                       target_requirement_ids)
        self.store.transition_run(run_id, "PLANNED", "design review said yes")
        plans = [(p, extra) for p, extra in self._worker_plans(
                     target_requirement_ids, turn_id, turn_n, directions or [])
                 if not strategies or p.strategy in strategies]
        self.store.set_run_difficulty(run_id, "directions", "auditor", len(plans))

        est = sum(self.budget.estimate_strategy(p.strategy) for p, _ in plans)
        decision = self.budget.check(est, session_start)
        if not decision.ok:
            self.store.transition_run(run_id, "BUDGET_REFUSED", decision.reason)
            log.warning("run %s refused by budget: %s", run_id, decision.reason)
            return run_id

        ex_ids: list[str] = []
        for plan, extra in plans:
            ex_ids.extend(self.executor.submit(
                plan, session_id, base_commit, samples=1, run_id=run_id,
                extra_prompt=extra))
        if not ex_ids:
            self.store.transition_run(run_id, "BUDGET_REFUSED",
                                      "no strategies planned")
            return run_id
        self.store.transition_run(
            run_id, "LAUNCHED",
            f"{len(ex_ids)} workers: "
            f"{', '.join(p.strategy for p, _ in plans)}")
        log.info("run %s launched: %d workers", run_id, len(ex_ids))
        return run_id

    def launch_synthesis(
        self,
        run,
        base_commit: str,
        plan_text: str,
        start_patch: Optional[str] = None,
    ) -> Optional[str]:
        """The final worker: continues a design, made on base_commit, following the
        plan. Returns the exploration id, or None when the budget refuses."""
        est = self.budget.estimate_strategy("council_synthesis")
        decision = self.budget.check(est)
        if not decision.ok:
            log.warning("final worker refused by budget: %s", decision.reason)
            return None
        # The turn whose code it starts from. A newer turn may still be in progress.
        turn = (self.store.turn_with_snapshot(run["session_id"], base_commit)
                or self.store.latest_turn(run["session_id"]))
        plan = PlannedExploration(
            strategy="council_synthesis",
            base_turn_id=turn["id"] if turn else "",
            base_turn_n=turn["n"] if turn else 0,
            target_ids=json.loads(run["target_requirement_ids_json"] or "[]"),
        )
        ex_ids = self.executor.submit(
            plan, run["session_id"], base_commit, samples=1,
            run_id=run["id"], mark_synthesis=True,
            extra_prompt={"directive": plan_text}, start_patch=start_patch)
        ex_id = ex_ids[0] if ex_ids else None
        log.info("run %s: final worker %s launched", run["id"], ex_id)
        return ex_id

    # ------------------------------------------------------------ internals

    def _worker_plans(self, target_ids: list[str], turn_id: str, turn_n: int,
                      directions: list[str]) -> list[tuple[PlannedExploration, dict]]:
        cap = max(self.cfg.experiments.workers_max - 1, 0)
        if len(directions) > cap:
            log.warning("%d directions exceed workers_max; keeping %d", len(directions), cap)
        named = [(f"direction_{i}", {"direction": text})
                 for i, text in enumerate(directions[:cap], start=1)]
        return [(PlannedExploration(strategy=name, base_turn_id=turn_id, base_turn_n=turn_n,
                                    target_ids=list(target_ids)), extra)
                for name, extra in [*named, (OPEN_WORKER, {})]]
