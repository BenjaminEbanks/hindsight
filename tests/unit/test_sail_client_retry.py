"""One dropped connection on a file write shouldn't end a run."""
from unittest import mock

import pytest

from hindsight import sail_client
from hindsight.sail_client import SailClient, SailClientError


def _client(side_effect):
    client = SailClient.__new__(SailClient)
    box = mock.Mock()
    box.fs.write_files.side_effect = side_effect
    client._box = lambda box_id: box
    return client, box


def test_write_retries_a_dropped_connection(monkeypatch):
    monkeypatch.setattr(sail_client.time, "sleep", lambda s: None)
    client, box = _client([OSError("Connection reset by peer"), None])
    client.write_files("b", {"/x": "y"})
    assert box.fs.write_files.call_count == 2


def test_write_gives_up_after_three_tries(monkeypatch):
    monkeypatch.setattr(sail_client.time, "sleep", lambda s: None)
    client, box = _client(OSError("Connection reset by peer"))
    with pytest.raises(SailClientError):
        client.write_files("b", {"/x": "y"})
    assert box.fs.write_files.call_count == 3


def test_write_does_not_retry_a_permanent_error(monkeypatch):
    monkeypatch.setattr(sail_client.time, "sleep", lambda s: None)
    client, box = _client(ValueError("permission denied"))
    with pytest.raises(SailClientError):
        client.write_files("b", {"/x": "y"})
    assert box.fs.write_files.call_count == 1


def test_create_box_waits_for_a_slot_at_either_limit(monkeypatch):
    monkeypatch.setattr(sail_client, "_BOX_SLOT_RETRY_SECONDS", 0)
    outcomes = [RuntimeError("your organization is at its limit of 200 reserved vCPUs"),
                RuntimeError("Sailbox concurrency limits are refreshing; retry shortly"),
                "box"]

    def create():
        outcome = outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    assert SailClient._when_a_slot_is_free(create, "x") == "box"
    assert outcomes == []


def test_create_box_does_not_wait_on_other_errors(monkeypatch):
    monkeypatch.setattr(sail_client, "_BOX_SLOT_RETRY_SECONDS", 0)
    calls = []

    def create():
        calls.append(1)
        raise RuntimeError("bad image")

    with pytest.raises(RuntimeError):
        SailClient._when_a_slot_is_free(create, "x")
    assert len(calls) == 1


def test_a_box_from_a_checkpoint_waits_for_a_slot_too(monkeypatch):
    monkeypatch.setattr(sail_client, "_BOX_SLOT_RETRY_SECONDS", 0)
    outcomes = [RuntimeError("at its limit of 200 reserved vCPUs"),
                mock.Mock(sailbox_id="b1")]

    def from_checkpoint(_id, name):
        outcome = outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(sail_client.Sailbox, "from_checkpoint", from_checkpoint)
    client = SailClient.__new__(SailClient)
    client.box_policy = None
    assert client.create_box("x", from_checkpoint_id="cp").id == "b1"
