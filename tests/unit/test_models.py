"""Model-call retry tests (2026-11-01: a single transient inference
timeout killed the only gate firing in the whole live eval run — every
model call now retries transient failures with backoff)."""

from __future__ import annotations

import pytest

from hindsight.models import _is_transient, make_model_fn


class FlakySail:
    """UNIT TEST MOCK: fails N times with a given error, then succeeds."""

    def __init__(self, failures: list[Exception]):
        self.failures = list(failures)
        self.calls = 0

    def inference_chat(self, model, messages, **kw):
        self.calls += 1
        if self.failures:
            raise self.failures.pop(0)
        return {"choices": [{"message": {"content": "ok"}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1}}


class FakeBudget:
    def record_model_cost(self, **kw):
        return 0.0


class FakeStatusError(Exception):
    def __init__(self, status: int):
        super().__init__(f"http {status}")
        self.status = status


def test_transient_errors_are_recognized():
    assert _is_transient(RuntimeError("request timed out: operation timed out"))
    assert _is_transient(RuntimeError("Connection reset by peer"))
    assert _is_transient(FakeStatusError(503))
    assert not _is_transient(RuntimeError("invalid api key"))
    assert not _is_transient(FakeStatusError(404))


def test_retries_transient_then_succeeds():
    sail = FlakySail([RuntimeError("request timed out")])
    fn = make_model_fn(sail, "m", FakeBudget(),
                            retry_delay_seconds=0.01)
    assert fn("prompt") == "ok"
    assert sail.calls == 2


def test_raises_after_exhausting_retries():
    sail = FlakySail([RuntimeError("operation timed out")] * 3)
    fn = make_model_fn(sail, "m", FakeBudget(),
                            retry_delay_seconds=0.01)
    with pytest.raises(RuntimeError):
        fn("prompt")
    assert sail.calls == 3  # initial + 2 retries


def test_permanent_error_raises_immediately():
    sail = FlakySail([RuntimeError("invalid api key")])
    fn = make_model_fn(sail, "m", FakeBudget(),
                            retry_delay_seconds=0.01)
    with pytest.raises(RuntimeError):
        fn("prompt")
    assert sail.calls == 1  # no retry on non-transient errors
