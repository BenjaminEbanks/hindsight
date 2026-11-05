"""One readable timeline of a session: what happened, in order, and why.

Built from what the store already records, so there is nothing extra to keep in
step. Start here when a run does something you did not expect.

    hindsight trace <session>             the timeline
    hindsight trace <session> --call 12   a model call's prompt and reply
    hindsight trace <session> --worker X  a worker's transcript
"""

from __future__ import annotations

import json
from typing import Any

from .store import Store

_WORKER_STATES = {"RUNNING", "GATING", "DISCARDED", "FAILED", "CANCELLED",
                  "SUPERSEDED", "NUDGE_READY", "SHOWN"}


def _short(text: Any, limit: int = 110) -> str:
    flat = " ".join(str(text or "").split())
    return flat if len(flat) <= limit else flat[:limit - 3] + "..."


def build_timeline(store: Store, session_id: str) -> list[dict]:
    """Every event as {ts, kind, run, worker, text}, oldest first."""
    events: list[dict] = []

    def add(ts, kind, text, run="", worker=""):
        if ts is not None:
            events.append({"ts": float(ts), "kind": kind, "run": run or "",
                           "worker": worker or "", "text": text})

    for m in store.user_messages_since(session_id, 0.0, limit=10_000):
        add(m["ts"], "message", _short(m["text"]))
    for t in store.turns_for_session(session_id, limit=10_000):
        usable = {1: "usable", 0: f"NOT usable: {t['usable_reason']}"}.get(
            t["usable"], "not settled")
        add(t["completed_at"], "turn", f"turn {t['n']} ended {t['status']}, {usable}")
    for c in store._query(
            "SELECT * FROM session_checks WHERE session_id=? ORDER BY ts", (session_id,)):
        reasons = "; ".join(json.loads(c["reasons_json"] or "[]")) or "no change needed"
        verdict = "START RUN" if c["worthy"] and c["priority"] == "raised" else "no run"
        add(c["ts"], "detect", f"{verdict}: {_short(reasons)}")

    workers: dict[str, str] = {}
    for run in store.runs_for_session(session_id):
        for r in store._query(
                "SELECT * FROM run_events WHERE run_id=? ORDER BY id", (run["id"],)):
            add(r["ts"], "run", f"{r['to_state']}: {_short(r['reason'])}", run=run["id"])
        for c in store._query("SELECT * FROM critiques WHERE run_id=?", (run["id"],)):
            add(c["ts"], "critique", f"{c['reviewer']} ranked {c['ranking_json']}",
                run=run["id"])
    for ex in store._query(
            "SELECT * FROM explorations WHERE session_id=? ORDER BY created_at",
            (session_id,)):
        workers[ex["id"]] = ex["strategy"]
        for e in store._query(
                "SELECT * FROM exploration_events WHERE exploration_id=? ORDER BY id",
                (ex["id"],)):
            if e["to_state"] in _WORKER_STATES:
                add(e["ts"], "worker",
                    f"{ex['strategy']} {e['to_state']}: {_short(e['reason'])}",
                    run=ex["run_id"], worker=ex["id"])
        for g in store._query(
                "SELECT g.* FROM gate_results g JOIN proposals p ON g.proposal_id=p.id "
                "WHERE p.exploration_id=? ORDER BY g.ts", (ex["id"],)):
            if g["stage"] == "1_worker_tests":  # information, not a check
                tests = json.loads(g["features_json"] or "{}")
                text = (f"{ex['strategy']} finished with {tests.get('passed')} tests "
                        f"passing, {tests.get('failed')} failing")
            else:
                text = (f"{ex['strategy']} {g['stage']} "
                        f"{'pass' if g['passed'] else 'FAIL'}: {_short(g['reason'])}")
            add(g["ts"], "check", text, run=ex["run_id"], worker=ex["id"])
        for n in store._query(
                "SELECT n.* FROM nudges n JOIN proposals p ON n.proposal_id=p.id "
                "WHERE p.exploration_id=?", (ex["id"],)):
            add(n["shown_at"], "deliver", f"released {n['id']}: {_short(n['text'])}",
                run=ex["run_id"], worker=ex["id"])
            add(n["agent_sent_at"], "deliver", f"handed to the main agent: {n['id']}",
                run=ex["run_id"], worker=ex["id"])
    for d in store._query(
            "SELECT d.* FROM deliveries d JOIN proposals p ON d.proposal_id=p.id "
            "JOIN explorations e ON p.exploration_id=e.id "
            "WHERE e.session_id=? AND d.held_reason != ''", (session_id,)):
        add(d["ts"], "deliver", f"held: {d['held_reason']}")
    for a in store._query(
            "SELECT * FROM adoptions WHERE session_id=?", (session_id,)):
        add(a["applied_at"], "adopt", f"applied {a['proposal_id']}, tests "
            f"{'passed' if a['test_passed'] else 'not passing'}")
        add(a["undone_at"], "adopt", f"undone {a['proposal_id']}")
    for c in store.model_calls(session_id):
        if c["seconds"] < 0:
            add(c["ts"], "model", f"#{c['id']} {c['step']} ({c['model']}) no reply yet",
                run=c["run_id"])
            continue
        status = f"ERROR {_short(c['error'], 70)}" if c["error"] else "ok"
        add(c["ts"], "model", f"#{c['id']} {c['step']} ({c['model']}, "
            f"{c['seconds']:.0f}s) {status}", run=c["run_id"])
    return sorted(events, key=lambda e: e["ts"])


def render_timeline(events: list[dict]) -> str:
    if not events:
        return "(nothing recorded for this session)"
    start = events[0]["ts"]
    lines = []
    for e in events:
        elapsed = int(e["ts"] - start)
        lines.append(f"+{elapsed // 60:>3}:{elapsed % 60:02d}  "
                     f"{e['kind']:<8}  {e['text']}")
    return "\n".join(lines)


def render_call(store: Store, call_id: int) -> str:
    c = store.model_call(call_id)
    if c is None:
        return f"(no model call #{call_id})"
    return (f"#{c['id']} {c['step']} prompt v{c['prompt_version']} on {c['model']}, "
            f"{'no reply yet' if c['seconds'] < 0 else format(c['seconds'], '.1f') + 's'}"
            f"\n\n=== PROMPT ===\n{c['prompt']}\n\n"
            f"=== REPLY ===\n{c['response']}"
            + (f"\n\n=== ERROR ===\n{c['error']}" if c["error"] else ""))
