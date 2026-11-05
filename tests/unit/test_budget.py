"""Budget manager tests."""

from __future__ import annotations

import time

import pytest

from hindsight.budget import BudgetManager, STRATEGY_ESTIMATE_USD
from hindsight.config import HindsightConfig
from hindsight.store import Store


@pytest.fixture()
def bm(tmp_path):
    store = Store(tmp_path / "db.sqlite3")
    yield BudgetManager(store, HindsightConfig())
    store.close()


def test_affordable_plan_ok(bm):
    d = bm.check(STRATEGY_ESTIMATE_USD["open_redesign"])
    assert d.ok


def test_daily_budget_refuses_large_plan(bm):
    from hindsight.store import CostEntry

    bm.store.record_cost(CostEntry(ref="x", kind="model", usd=39.0))
    d = bm.check(STRATEGY_ESTIMATE_USD["open_redesign"])
    assert not d.ok
    assert "daily" in d.reason


def test_session_budget_respected(bm):
    from hindsight.store import CostEntry

    cfg = HindsightConfig()
    start = time.time() - 10
    bm.store.record_cost(CostEntry(ref="x", kind="model", usd=cfg.budget.per_session_usd))
    d = bm.check(1.0, session_started_ts=start)
    assert not d.ok


def test_token_cost(bm):
    assert bm.token_cost_usd("zai-org/GLM-5.3", 1_000_000, 0) == pytest.approx(0.6)
    assert bm.token_cost_usd("zai-org/GLM-5.3-Flash", 1_000_000, 1_000_000) == pytest.approx(0.5)
