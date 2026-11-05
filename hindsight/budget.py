"""Budget and cost manager.

Unit prices are estimates. Costs are computed from the usage fields in API responses
and from box run time, and are not reconciled against billing.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from .config import HindsightConfig
from .store import CostEntry, Store

# Fixed cost estimates per exploration (USD), used to decide whether a run fits the budget.
STRATEGY_ESTIMATE_USD: dict[str, float] = {
    # Guesses, reconciled against billing later. Direction workers use the default.
    "open_redesign": 1.50,
    "council_synthesis": 1.50,
}

# Estimated token prices (USD per million tokens). These drive the budget limits and
# are not reconciled against billing. Add your own with [models.prices].
MODEL_PRICES: dict[str, tuple[float, float]] = {
    # model substring -> (input $/M tokens, output $/M tokens)
    "GLM-5.3-Flash": (0.1, 0.4),
    "Gemma-4-31B": (0.11, 0.32),  # Sail's listed balanced price, 2026-11-03
    "GLM-5.3": (0.6, 2.2),
    "Kimi-K3": (0.6, 2.5),
    "DeepSeek-V4-Pro": (0.6, 2.2),
    "DeepSeek": (0.15, 0.6),
}
DEFAULT_PRICE = (1.0, 5.0)


@dataclass
class BudgetDecision:
    ok: bool
    reason: str
    estimated_cost: float
    session_spend: float
    day_spend: float


class BudgetManager:
    def __init__(self, store: Store, cfg: HindsightConfig):
        self.store = store
        self.cfg = cfg

    @staticmethod
    def token_cost_usd(model: str, input_tokens: int, output_tokens: int,
                       prices: dict[str, tuple[float, float]] | None = None) -> float:
        for key in sorted(prices or {}, key=len, reverse=True):
            if key in model:
                pin, pout = prices[key]
                return input_tokens / 1e6 * pin + output_tokens / 1e6 * pout
        # longest key first so "gpt-5-mini" is not swallowed by "gpt-5"
        for key in sorted(MODEL_PRICES, key=len, reverse=True):
            if key in model:
                pin, pout = MODEL_PRICES[key]
                return input_tokens / 1e6 * pin + output_tokens / 1e6 * pout
        pin, pout = DEFAULT_PRICE
        return input_tokens / 1e6 * pin + output_tokens / 1e6 * pout

    def estimate_strategy(self, strategy: str) -> float:
        return STRATEGY_ESTIMATE_USD.get(strategy, 1.0)

    def check(self, plan_cost_usd: float, session_started_ts: float | None = None) -> BudgetDecision:
        day_start = time.time() - 24 * 3600
        day_spend = self.store.spend_since(day_start)
        if day_spend >= self.cfg.budget.per_day_usd:
            return BudgetDecision(False, f"daily budget exhausted (${day_spend:.2f} of "
                                         f"${self.cfg.budget.per_day_usd:.2f})", plan_cost_usd,
                                  self._session_spend(session_started_ts), day_spend)
        if day_spend + plan_cost_usd > self.cfg.budget.per_day_usd:
            return BudgetDecision(False, f"plan cost ${plan_cost_usd:.2f} exceeds remaining daily "
                                         f"budget (${self.cfg.budget.per_day_usd - day_spend:.2f})",
                                  plan_cost_usd, self._session_spend(session_started_ts), day_spend)
        if session_started_ts is not None:
            session_spend = self.store.spend_since(session_started_ts)
            if session_spend + plan_cost_usd > self.cfg.budget.per_session_usd:
                return BudgetDecision(False, f"plan cost exceeds per-session budget "
                                             f"(${session_spend:.2f} spent of "
                                             f"${self.cfg.budget.per_session_usd:.2f})",
                                      plan_cost_usd, session_spend, day_spend)
        return BudgetDecision(True, "ok", plan_cost_usd,
                              self._session_spend(session_started_ts), day_spend)

    def _session_spend(self, session_started_ts: float | None) -> float:
        return self.store.spend_since(session_started_ts) if session_started_ts else 0.0

    def record_model_cost(self, ref: str, model: str, input_tokens: int, output_tokens: int,
                          tier: str = "standard") -> float:
        usd = self.token_cost_usd(model, input_tokens, output_tokens, self.cfg.models.prices)
        self.store.record_cost(CostEntry(
            ref=ref, kind="model", model=model, tier=tier,
            input_tokens=input_tokens, output_tokens=output_tokens, usd=usd))
        return usd

    def record_box_cost(self, ref: str, box_seconds: float, price_per_box_minute: float = 0.002) -> float:
        """Box cost from measured run time, at an estimated price."""
        usd = box_seconds / 60.0 * price_per_box_minute
        self.store.record_cost(CostEntry(ref=ref, kind="box", box_seconds=box_seconds, usd=usd))
        return usd
