"""Fakes answer at once, so tests have no reason to wait out real polling."""

import pytest


@pytest.fixture(autouse=True)
def _no_real_polling(monkeypatch):
    monkeypatch.setattr("hindsight.resume._POLL_SECONDS", 0.01)
