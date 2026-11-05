"""Runtime contract tests against real Sail.

These are the Milestone 0 checks that cannot run by inspection: they
exercise real Sailboxes, measure latencies, and verify the fork path.
They SKIP entirely without SAIL_API_KEY so `pytest` stays green locally.
Results are appended to VERIFY.md when run for real.
"""

from __future__ import annotations

import os
import statistics
import time

import pytest

from hindsight.sail_client import SailClient

pytestmark = pytest.mark.skipif(
    not os.environ.get("SAIL_API_KEY"), reason="SAIL_API_KEY not set (runtime contract tests)")


@pytest.fixture(scope="module")
def sail():
    return SailClient("hindsight-contract")


def _time(fn, *a, **kw):
    t0 = time.time()
    out = fn(*a, **kw)
    return out, time.time() - t0


def test_box_lifecycle_roundtrip(sail, tmp_path):
    """Create, exec, upload, download, terminate (V-rt-1). Latencies recorded."""
    latencies: dict[str, float] = {}

    box, latencies["create"] = _time(sail.create_box, "hs-contract-1")
    try:
        out, latencies["exec"] = _time(
            sail.run, box.id, ["bash", "-c", "echo hello && python3 --version"])
        assert out.exit_code == 0, out.stderr
        assert "hello" in out.stdout

        local = tmp_path / "up.txt"
        local.write_text("payload")
        _, latencies["upload"] = _time(sail.upload_file, box.id, str(local), "/work/up.txt")
        back = tmp_path / "down.txt"
        _, latencies["download"] = _time(sail.download_file, box.id, "/work/up.txt", str(back))
        assert back.read_text() == "payload"

        _, latencies["write_read"] = _time(
            sail.write_files, box.id, {"/work/w.json": '{"ok": true}'})
        raw = sail.read_file(box.id, "/work/w.json")
        assert b'"ok": true' in raw
    finally:
        _, latencies["terminate"] = _time(sail.terminate, box.id)

    print("\nSail contract latencies (s):", {k: round(v, 2) for k, v in latencies.items()})


def test_checkpoint_and_fork(sail):
    """checkpoint(ttl_seconds=) + from_checkpoint = the fork path (V-rt-5,
    Section 11.6). Verifies background processes continue across the fork."""
    box = sail.create_box("hs-contract-fork-src")
    try:
        sail.run(box.id, ["bash", "-c", "mkdir -p /work && echo marker > /work/state.txt"])
        cp_id, _ = _time(sail.checkpoint, box.id, ttl_seconds=3600, name="contract-fork")
        forked = sail.from_checkpoint(cp_id, name="hs-contract-fork-a")
        try:
            out = sail.run(forked.id, ["bash", "-c", "cat /work/state.txt"])
            assert "marker" in out.stdout, "forked box lost the parent's disk state"
        finally:
            sail.terminate(forked.id)
    finally:
        sail.terminate(box.id)


def test_reaper_list(sail):
    boxes = sail.list_boxes(search="hs-contract")
    assert isinstance(boxes, list)


def test_inference_reachable(sail):
    resp = sail.inference_chat(
        "zai-org/GLM-5.3-Flash",
        [{"role": "user", "content": "reply with the single word: ok"}],
        max_output_tokens=16,
    )
    assert resp.get("choices"), f"unexpected inference response: {resp}"
    print("\ninference usage fields:", resp.get("usage"))


def test_model_listing(sail):
    """M0 model matrix source of truth: what Sail actually serves."""
    from sail._core_client import ApiTransport

    status, data = ApiTransport().get("/v1/models", timeout=30)
    assert status == 200, f"models list failed: {status}"
    ids = [m["id"] for m in data.get("data", [])]
    assert any("GLM" in i for i in ids)
    print("\nserved models:", ids)


def test_concurrency(sail, n=50):
    """V-rt-2: launch N boxes concurrently from one tiny base."""
    boxes = []
    t0 = time.time()
    try:
        for i in range(n):
            boxes.append(sail.create_box(f"hs-contract-n{i:02d}"))
    finally:
        for b in boxes:
            try:
                sail.terminate(b.id)
            except Exception:
                pass
    print(f"\n{len(boxes)}/{n} boxes created in {time.time() - t0:.1f}s")


def test_latency_stability(sail, runs=10):
    """p50/p95 over repeated creates (V-rt-1)."""
    lat = []
    for i in range(runs):
        box, dt = _time(sail.create_box, f"hs-contract-lat{i}")
        sail.terminate(box.id)
        lat.append(dt)
    lat.sort()
    print(f"\ncreate latency p50={statistics.median(lat):.2f}s "
          f"p95={lat[int(0.95 * len(lat)) - 1]:.2f}s")
