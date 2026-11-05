"""SQLite event store.

All state is reconstructible from this store, so the orchestrator can crash
and resume. Large payloads go to a blob directory; small ones are inlined.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Iterable, Optional

from pydantic import BaseModel

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
  id TEXT PRIMARY KEY, project TEXT, harness TEXT, harness_version TEXT,
  started_at REAL, ended_at REAL);
CREATE TABLE IF NOT EXISTS turns (
  id TEXT PRIMARY KEY, session_id TEXT, n INTEGER, user_message_id TEXT,
  status TEXT, pre_snapshot TEXT, post_snapshot TEXT, completed_at REAL);
CREATE TABLE IF NOT EXISTS messages (
  id TEXT PRIMARY KEY, session_id TEXT, turn_id TEXT, role TEXT,
  raw_blob TEXT, ts REAL);
CREATE TABLE IF NOT EXISTS manual_edits (
  turn_id TEXT, path TEXT, lines_added INTEGER, lines_removed INTEGER, hunks_blob TEXT);
CREATE TABLE IF NOT EXISTS turn_files (
  turn_id TEXT, path TEXT);
CREATE INDEX IF NOT EXISTS idx_turn_files_turn ON turn_files (turn_id);
CREATE TABLE IF NOT EXISTS ledger_versions (
  session_id TEXT, version INTEGER, turn_id TEXT, ledger_blob TEXT,
  patch_blob TEXT, model TEXT, cost_usd REAL, PRIMARY KEY (session_id, version));
CREATE TABLE IF NOT EXISTS explorations (
  id TEXT PRIMARY KEY, session_id TEXT, strategy TEXT, harness TEXT,
  base_turn_id TEXT, ledger_version INTEGER, state TEXT, budget_json TEXT,
  box_id TEXT, run_id TEXT, checkpoint_id TEXT,
  created_at REAL, finished_at REAL, exit_reason TEXT);
CREATE TABLE IF NOT EXISTS exploration_events (
  id INTEGER PRIMARY KEY, exploration_id TEXT, from_state TEXT, to_state TEXT,
  reason TEXT, ts REAL);
CREATE TABLE IF NOT EXISTS proposals (
  id TEXT PRIMARY KEY, exploration_id TEXT, patch_blob TEXT, summary_json TEXT,
  rebased_onto TEXT, rebase_status TEXT, qualified INTEGER,
  regressions_json TEXT, requirement_flips_json TEXT, created_at REAL);
CREATE TABLE IF NOT EXISTS gate_results (
  proposal_id TEXT, stage TEXT, passed INTEGER, features_json TEXT,
  reason TEXT, ts REAL);
CREATE TABLE IF NOT EXISTS nudges (
  id TEXT PRIMARY KEY, proposal_id TEXT, tier TEXT, text TEXT,
  shown_at REAL, channel TEXT);
CREATE TABLE IF NOT EXISTS deliveries (
  id TEXT PRIMARY KEY, proposal_id TEXT, shown_at REAL,
  channel TEXT, held_reason TEXT, ts REAL);
CREATE TABLE IF NOT EXISTS dismissals (
  id TEXT PRIMARY KEY, session_id TEXT, fingerprint TEXT,
  reason TEXT, ts REAL);
CREATE TABLE IF NOT EXISTS adoptions (
  id TEXT PRIMARY KEY, proposal_id TEXT, session_id TEXT,
  backup_dir TEXT, files_json TEXT, created_files_json TEXT,
  test_command TEXT, test_passed INTEGER, tests_json TEXT,
  notified INTEGER, notify_channel TEXT, applied_at REAL,
  undone_at REAL, created_at REAL);
CREATE TABLE IF NOT EXISTS user_actions (
  id INTEGER PRIMARY KEY, target TEXT, action TEXT, reason TEXT, via TEXT, ts REAL);
CREATE TABLE IF NOT EXISTS costs (
  id INTEGER PRIMARY KEY, ref TEXT, kind TEXT, model TEXT, tier TEXT,
  input_tokens INTEGER, output_tokens INTEGER, box_seconds REAL, usd REAL, ts REAL);
CREATE TABLE IF NOT EXISTS envs (
  key TEXT PRIMARY KEY, checkpoint_id TEXT, expires_at REAL,
  setup_json TEXT, flaky_tests_json TEXT);
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS session_checks (
  id INTEGER PRIMARY KEY, session_id TEXT, turn_id TEXT, worthy INTEGER,
  priority TEXT, checks_json TEXT, reasons_json TEXT, ts REAL);
CREATE TABLE IF NOT EXISTS runs (
  id TEXT PRIMARY KEY, session_id TEXT, ledger_version INTEGER,
  edge_case_ids_json TEXT, target_requirement_ids_json TEXT,
  difficulty TEXT, difficulty_source TEXT, n_workers INTEGER,
  state TEXT, heldout_json TEXT, winner_proposal_id TEXT,
  created_at REAL, finished_at REAL, exit_reason TEXT);
CREATE TABLE IF NOT EXISTS run_events (
  id INTEGER PRIMARY KEY, run_id TEXT, from_state TEXT, to_state TEXT,
  reason TEXT, ts REAL);
CREATE TABLE IF NOT EXISTS critiques (
  id INTEGER PRIMARY KEY, run_id TEXT, reviewer TEXT,
  labels_json TEXT, ranking_json TEXT, raw TEXT, model TEXT, ts REAL);
CREATE TABLE IF NOT EXISTS model_calls (
  id INTEGER PRIMARY KEY, session_id TEXT, run_id TEXT, step TEXT, model TEXT,
  prompt_version TEXT, prompt_blob TEXT, response_blob TEXT, error TEXT,
  seconds REAL, ts REAL);
CREATE TABLE IF NOT EXISTS worker_transcripts (
  exploration_id TEXT PRIMARY KEY, transcript_blob TEXT);
CREATE INDEX IF NOT EXISTS idx_turns_session ON turns (session_id, n);
CREATE INDEX IF NOT EXISTS idx_messages_turn ON messages (turn_id);
CREATE INDEX IF NOT EXISTS idx_explorations_session ON explorations (session_id, created_at);
CREATE INDEX IF NOT EXISTS idx_proposals_exploration ON proposals (exploration_id);
CREATE INDEX IF NOT EXISTS idx_session_checks_session ON session_checks (session_id, ts);
CREATE INDEX IF NOT EXISTS idx_runs_session ON runs (session_id, created_at);
CREATE INDEX IF NOT EXISTS idx_explorations_run ON explorations (run_id);
"""

# Column migrations for databases created before v2 (spec Step 0/1).
_TURN_MIGRATIONS = [
    ("usable", "ALTER TABLE turns ADD COLUMN usable INTEGER"),
    ("usable_reason", "ALTER TABLE turns ADD COLUMN usable_reason TEXT"),
    ("snapshot_files", "ALTER TABLE turns ADD COLUMN snapshot_files INTEGER"),
]
_EXPLORATION_MIGRATIONS = [
    ("run_id", "ALTER TABLE explorations ADD COLUMN run_id TEXT"),
    ("checkpoint_id", "ALTER TABLE explorations ADD COLUMN checkpoint_id TEXT"),
]
_RUN_MIGRATIONS = [
    ("winner_proposal_id", "ALTER TABLE runs ADD COLUMN winner_proposal_id TEXT"),
    # Council: the chairman's directive + rationale + aggregate
    # ranking, stored on the run between the critique round and the synthesis
    # worker's return.
    ("chairman_json", "ALTER TABLE runs ADD COLUMN chairman_json TEXT"),
]
# M-G: the plugin toast acks released nudges (shown_at = released by the
# delivery engine; acked_at = the plugin displayed it).
_NUDGE_MIGRATIONS = [
    ("acked_at", "ALTER TABLE nudges ADD COLUMN acked_at REAL"),
    ("ack_displayed", "ALTER TABLE nudges ADD COLUMN ack_displayed INTEGER"),
    ("agent_sent_at", "ALTER TABLE nudges ADD COLUMN agent_sent_at REAL"),
    ("missed_json", "ALTER TABLE nudges ADD COLUMN missed_json TEXT"),
]
_PROPOSAL_MIGRATIONS = [
    ("qualified", "ALTER TABLE proposals ADD COLUMN qualified INTEGER"),
    ("regressions_json", "ALTER TABLE proposals ADD COLUMN regressions_json TEXT"),
    ("requirement_flips_json",
     "ALTER TABLE proposals ADD COLUMN requirement_flips_json TEXT"),
]

# Exploration state machine.
NON_TERMINAL = {"PENDING", "PROVISIONING", "RUNNING", "TESTING", "GATING", "NUDGE_READY", "SHOWN"}
TERMINAL = {"DISCARDED", "INBOX", "ADOPTED", "DISMISSED", "EXPIRED", "CANCELLED", "FAILED", "SUPERSEDED"}
ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    "PENDING": {"PROVISIONING", "CANCELLED", "FAILED", "SUPERSEDED"},
    "PROVISIONING": {"RUNNING", "CANCELLED", "FAILED", "SUPERSEDED"},
    "RUNNING": {"TESTING", "CANCELLED", "FAILED", "SUPERSEDED"},
    "TESTING": {"GATING", "FAILED", "SUPERSEDED"},
    "GATING": {"DISCARDED", "INBOX", "NUDGE_READY", "FAILED", "SUPERSEDED"},
    "INBOX": {"ADOPTED", "DISMISSED", "EXPIRED"},
    "NUDGE_READY": {"SHOWN", "SUPERSEDED"},
    "SHOWN": {"ADOPTED", "DISMISSED", "INBOX"},
}
for _s in NON_TERMINAL:
    ALLOWED_TRANSITIONS.setdefault(_s, set()).add("SUPERSEDED")

# Run lifecycle:
#   NEW -> PLANNED -> LAUNCHED -> SELECTING <-> READY_TO_DELIVER
#             ↓          ↓           ↓             ↓
#     BUDGET_REFUSED  ALL_FAILED  NONE_QUALIFIED  DELIVERED | DROPPED_STALE
# READY_TO_DELIVER returns to SELECTING when a user message arrives after the
# winner finished: it goes back to that worker before anything is sent.
RUN_NON_TERMINAL = {"NEW", "PLANNED", "LAUNCHED", "SELECTING",
                    "READY_TO_DELIVER"}
RUN_TERMINAL = {"BUDGET_REFUSED", "ALL_FAILED", "NONE_QUALIFIED",
                "DELIVERED", "DROPPED_STALE"}

RUN_TRANSITIONS: dict[str, set[str]] = {
    "NEW": {"PLANNED"},
    "PLANNED": {"LAUNCHED", "BUDGET_REFUSED"},
    "LAUNCHED": {"SELECTING", "ALL_FAILED"},
    # ALL_FAILED is reachable from SELECTING too: "every worker failed" is
    # discovered when selection finds no alive proposals (workers can fail
    # between finishing and proposal collection).
    "SELECTING": {"READY_TO_DELIVER", "NONE_QUALIFIED", "DROPPED_STALE",
                  "ALL_FAILED"},
    "READY_TO_DELIVER": {"DELIVERED", "DROPPED_STALE", "SELECTING"},
}


class InvalidTransition(Exception):
    pass


class CostEntry(BaseModel):
    ref: str
    kind: str  # model | box
    model: Optional[str] = None
    tier: Optional[str] = None
    input_tokens: int = 0
    output_tokens: int = 0
    box_seconds: float = 0.0
    usd: float = 0.0


def built_on_key(exploration_id: str) -> str:
    """The commit a worker's code started from, whatever turn is latest by then."""
    return f"built_on:{exploration_id}"


class Store:
    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.blob_dir = self.db_path.parent / "blobs"
        self.blob_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.executescript(SCHEMA)
            existing = {r["name"] for r in self._conn.execute(
                "PRAGMA table_info(turns)").fetchall()}
            for col, sql in _TURN_MIGRATIONS:
                if col not in existing:
                    self._conn.execute(sql)
            existing_ex = {r["name"] for r in self._conn.execute(
                "PRAGMA table_info(explorations)").fetchall()}
            for col, sql in _EXPLORATION_MIGRATIONS:
                if col not in existing_ex:
                    self._conn.execute(sql)
            existing_pr = {r["name"] for r in self._conn.execute(
                "PRAGMA table_info(proposals)").fetchall()}
            for col, sql in _PROPOSAL_MIGRATIONS:
                if col not in existing_pr:
                    self._conn.execute(sql)
            existing_ru = {r["name"] for r in self._conn.execute(
                "PRAGMA table_info(runs)").fetchall()}
            for col, sql in _RUN_MIGRATIONS:
                if col not in existing_ru:
                    self._conn.execute(sql)
            existing_nu = {r["name"] for r in self._conn.execute(
                "PRAGMA table_info(nudges)").fetchall()}
            for col, sql in _NUDGE_MIGRATIONS:
                if col not in existing_nu:
                    self._conn.execute(sql)
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ---------------------------------------------------------------- blobs

    def _blob_set(self, value: Any) -> str:
        data = json.dumps(value, sort_keys=True) if not isinstance(value, str) else value
        if len(data) <= 32_768:
            return data
        name = hashlib.sha256(data.encode()).hexdigest()[:32]
        (self.blob_dir / name).write_text(data)
        return f"file:{name}"

    def _blob_get(self, ref: str) -> str:
        if ref.startswith("file:"):
            return (self.blob_dir / ref[5:]).read_text()
        return ref

    # -------------------------------------------------------------- helpers

    def _exec(self, sql: str, params: tuple = ()) -> None:
        with self._lock:
            self._conn.execute(sql, params)
            self._conn.commit()

    def _query(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    def _query_one(self, sql: str, params: tuple = ()) -> Optional[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, params).fetchone()

    # ------------------------------------------------------------- sessions

    def upsert_session(self, session_id: str, project: str, harness: str, harness_version: str) -> None:
        row = self._query_one("SELECT id FROM sessions WHERE id=?", (session_id,))
        if row is None:
            self._exec(
                "INSERT INTO sessions (id, project, harness, harness_version, started_at) VALUES (?,?,?,?,?)",
                (session_id, project, harness, harness_version, time.time()),
            )

    def end_session(self, session_id: str) -> None:
        self._exec("UPDATE sessions SET ended_at=? WHERE id=?", (time.time(), session_id))

    # ---------------------------------------------------------------- turns

    def start_turn(self, session_id: str, n: int, user_message_id: str, pre_snapshot: str | None) -> str:
        turn_id = f"turn-{uuid.uuid4().hex[:12]}"
        self._exec(
            "INSERT INTO turns (id, session_id, n, user_message_id, status, pre_snapshot) VALUES (?,?,?,?,?,?)",
            (turn_id, session_id, n, user_message_id, "in_progress", pre_snapshot),
        )
        return turn_id

    def complete_turn(
        self, turn_id: str, status: str, post_snapshot: str | None,
        snapshot_files: int | None = None,
    ) -> None:
        self._exec(
            "UPDATE turns SET status=?, post_snapshot=?, snapshot_files=?, completed_at=? WHERE id=?",
            (status, post_snapshot, snapshot_files, time.time(), turn_id),
        )

    def mark_turn_usable(self, turn_id: str, usable: bool, reason: str) -> None:
        """Settle-guard verdict."""
        self._exec(
            "UPDATE turns SET usable=?, usable_reason=? WHERE id=?",
            (int(usable), reason, turn_id),
        )

    def turn_with_snapshot(self, session_id: str, commit: str) -> Optional[sqlite3.Row]:
        rows = self._query("SELECT * FROM turns WHERE session_id=? AND post_snapshot=? "
                           "ORDER BY n DESC LIMIT 1", (session_id, commit))
        return rows[0] if rows else None

    def latest_usable_snapshot(self, session_id: str) -> Optional[sqlite3.Row]:
        """Latest post-turn snapshot that settled cleanly on a completed turn."""
        return self._query_one(
            "SELECT * FROM turns WHERE session_id=? AND post_snapshot IS NOT NULL "
            "AND usable=1 AND status='ok' ORDER BY n DESC LIMIT 1", (session_id,)
        )

    def last_activity_ts(self, session_id: str) -> float:
        """Most recent message or turn-completion time for the session."""
        row = self._query_one(
            "SELECT MAX(ts) AS m FROM messages WHERE session_id=?", (session_id,))
        trow = self._query_one(
            "SELECT MAX(completed_at) AS c FROM turns WHERE session_id=?", (session_id,))
        srow = self._query_one(
            "SELECT started_at AS s FROM sessions WHERE id=?", (session_id,))
        candidates = [r for r in (row["m"] if row else None,
                                  trow["c"] if trow else None,
                                  srow["s"] if srow else None) if r]
        return max(candidates) if candidates else 0.0

    # --------------------------------------------------------- session gate

    def save_session_check(self, session_id: str, turn_id: str | None, worthy: bool,
                          priority: str, checks: dict, reasons: list[str]) -> None:
        self._exec(
            "INSERT INTO session_checks (session_id, turn_id, worthy, priority, "
            "checks_json, reasons_json, ts) VALUES (?,?,?,?,?,?,?)",
            (session_id, turn_id, int(worthy), priority,
             json.dumps(checks), json.dumps(reasons), time.time()),
        )

    def latest_session_check(self, session_id: str) -> Optional[sqlite3.Row]:
        return self._query_one(
            "SELECT * FROM session_checks WHERE session_id=? ORDER BY ts DESC LIMIT 1",
            (session_id,))

    # ------------------------------------------------------------------ runs

    def create_run(self, session_id: str, ledger_version: int,
                   target_requirement_ids: list[str]) -> str:
        run_id = f"run-{uuid.uuid4().hex[:10]}"
        self._exec(
            "INSERT INTO runs (id, session_id, ledger_version, "
            "target_requirement_ids_json, state, created_at) VALUES (?,?,?,?,?,?)",
            (run_id, session_id, ledger_version,
             json.dumps(target_requirement_ids), "NEW", time.time()),
        )
        self._record_run_event(run_id, None, "NEW", "created")
        return run_id

    def _record_run_event(self, run_id: str, frm: Optional[str], to: str,
                          reason: str) -> None:
        self._exec(
            "INSERT INTO run_events (run_id, from_state, to_state, reason, ts) "
            "VALUES (?,?,?,?,?)",
            (run_id, frm, to, reason, time.time()),
        )

    def transition_run(self, run_id: str, to_state: str, reason: str = "") -> None:
        row = self._query_one("SELECT state, session_id FROM runs WHERE id=?",
                              (run_id,))
        if row is None:
            raise InvalidTransition(f"unknown run {run_id}")
        frm = row["state"]
        if to_state not in RUN_TRANSITIONS.get(frm, set()):
            raise InvalidTransition(
                f"illegal run transition {frm} -> {to_state} for {run_id} ({reason})")
        self._exec(
            "UPDATE runs SET state=?, exit_reason=? WHERE id=?",
            (to_state, reason if to_state in RUN_TERMINAL else "", run_id))
        if to_state in RUN_TERMINAL:
            self._exec("UPDATE runs SET finished_at=? WHERE id=?", (time.time(), run_id))
            latest = self._query_one(
                "SELECT MAX(version) AS v FROM ledger_versions WHERE session_id=?",
                (row["session_id"],))
            if latest and latest["v"] is not None:
                self.kv_set(EXAMINED_KEY.format(row["session_id"]), str(latest["v"]))
        self._record_run_event(run_id, frm, to_state, reason)

    # Council: critiques + chairman directive ---------------

    def save_critique(self, run_id: str, reviewer: str,
                      labels: dict, ranking: list[str], raw: str,
                      model: str = "") -> None:
        self._exec(
            "INSERT INTO critiques (run_id, reviewer, labels_json, "
            "ranking_json, raw, model, ts) VALUES (?,?,?,?,?,?,?)",
            (run_id, reviewer, json.dumps(labels),
             json.dumps(ranking), raw, model, time.time()))

    def run_critiques(self, run_id: str) -> list[dict]:
        rows = self._query(
            "SELECT * FROM critiques WHERE run_id=? ORDER BY ts", (run_id,))
        return [dict(r) for r in rows]

    def set_run_chairman(self, run_id: str, chairman: dict) -> None:
        self._exec("UPDATE runs SET chairman_json=? WHERE id=?",
                   (json.dumps(chairman), run_id))

    def get_run_chairman(self, run_id: str) -> Optional[dict]:
        row = self._query_one("SELECT chairman_json FROM runs WHERE id=?",
                              (run_id,))
        if row is None or not row["chairman_json"]:
            return None
        try:
            return json.loads(row["chairman_json"])
        except ValueError:
            return None

    # One-run-at-a-time: a non-terminal run still COMPUTING
    # blocks new launches in the same session. READY_TO_DELIVER counts: a run
    # is ongoing until its result has been sent or dropped, and detection
    # resumes only then. Non-terminal rows older than the grace window are
    # corpses (killed mid-flight) that must not wedge the session.
    _RUN_ACTIVE_STATES = ("NEW", "PLANNED", "LAUNCHED", "SELECTING",
                          "READY_TO_DELIVER")
    _RUN_ACTIVE_GRACE_S = 6 * 3600

    def session_has_active_run(self, session_id: str,
                               now: Optional[float] = None) -> bool:
        now = time.time() if now is None else now
        row = self._query_one(
            "SELECT COUNT(*) AS n FROM runs WHERE session_id=? AND state IN ("
            + ",".join(f"'{s}'" for s in self._RUN_ACTIVE_STATES)
            + ") AND created_at > ?",
            (session_id, now - self._RUN_ACTIVE_GRACE_S))
        return bool(row and row["n"])

    # Worker updates stream: new USER messages pushed into
    # in-flight boxes as /work/updates/u-<n>.json.

    def user_messages_since(self, session_id: str, since_ts: float,
                            limit: int = 10) -> list[dict]:
        rows = self._query(
            "SELECT ts, raw_blob FROM messages WHERE session_id=? AND "
            "role='user' AND ts>? ORDER BY ts LIMIT ?",
            (session_id, since_ts, limit))
        out = []
        for r in rows:
            try:
                blob = json.loads(self._blob_get(r["raw_blob"]))
            except ValueError:
                continue
            text = str(blob.get("text") or "")
            if text:
                out.append({"ts": r["ts"], "text": text})
        return out

    def latest_user_message_id_at(self, session_id: str, ts: float) -> int:
        """Row id of the newest user message at-or-before ts (0 when none) —
        the watermark floor for update streaming: a worker's launch
        transcript already covers these."""
        row = self._query_one(
            "SELECT rowid AS rid FROM messages WHERE session_id=? AND "
            "role='user' AND ts<=? ORDER BY rowid DESC LIMIT 1",
            (session_id, ts))
        return int(row["rid"]) if row else 0

    def user_messages_after(self, session_id: str, after_id: int,
                            limit: int = 10) -> list[dict]:
        """User messages with rowid > after_id, id-ordered. the
        watermark key is the message ROW ID, not a timestamp — two messages
        in one millisecond must never collide (each becomes its own
        /work/updates/u-<id>.json file in the worker box)."""
        rows = self._query(
            "SELECT rowid AS rid, ts, raw_blob FROM messages WHERE "
            "session_id=? AND role='user' AND rowid>? ORDER BY rowid LIMIT ?",
            (session_id, after_id, limit))
        out = []
        for r in rows:
            try:
                blob = json.loads(self._blob_get(r["raw_blob"]))
            except ValueError:
                continue
            text = str(blob.get("text") or "")
            if text:
                out.append({"id": int(r["rid"]), "ts": r["ts"], "text": text})
        return out

    def last_user_message_id(self, session_id: str) -> int:
        row = self._query_one(
            "SELECT MAX(rowid) AS rid FROM messages WHERE session_id=? AND "
            "role='user'", (session_id,))
        return int(row["rid"] or 0) if row else 0

    def messages_unseen_by(self, exploration_id: str) -> list[dict]:
        """User messages stored after the last one this worker was given. Keyed
        on row id, not timestamp: a message is stored when its turn ends, so its
        timestamp can predate a worker's finish without the worker ever seeing it."""
        row = self.get_exploration(exploration_id)
        if row is None:
            return []
        raw = self.kv_get(f"updates_wm:{exploration_id}")
        after = int(raw) if raw is not None else self.latest_user_message_id_at(
            row["session_id"], float(row["created_at"] or 0))
        return self.user_messages_after(row["session_id"], after, limit=50)

    def save_model_call(self, *, session_id: str, run_id: str, step: str, model: str,
                        prompt_version: str, prompt: str, response: str, error: str,
                        seconds: float) -> int:
        with self._lock:
            self._exec(
                "INSERT INTO model_calls (session_id, run_id, step, model, prompt_version, "
                "prompt_blob, response_blob, error, seconds, ts) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (session_id, run_id, step, model, prompt_version, self._blob_set(prompt),
                 self._blob_set(response), error, seconds, time.time()))
            return int(self._conn.execute("SELECT last_insert_rowid()").fetchone()[0])

    def finish_model_call(self, call_id: int, *, response: str, error: str,
                          seconds: float) -> None:
        self._exec("UPDATE model_calls SET response_blob=?, error=?, seconds=? WHERE id=?",
                   (self._blob_set(response), error, seconds, call_id))

    def model_calls(self, session_id: str) -> list[sqlite3.Row]:
        """Without their text: the timeline only needs the outline."""
        return self._query(
            "SELECT id, run_id, step, model, error, seconds, ts FROM model_calls "
            "WHERE session_id=? OR run_id IN "
            "(SELECT id FROM runs WHERE session_id=?) ORDER BY id",
            (session_id, session_id))

    def model_call(self, call_id: int) -> Optional[dict]:
        row = self._query_one("SELECT * FROM model_calls WHERE id=?", (call_id,))
        if row is None:
            return None
        return {**dict(row), "prompt": self._blob_get(row["prompt_blob"]),
                "response": self._blob_get(row["response_blob"])}

    def save_worker_transcript(self, exploration_id: str, transcript: str) -> None:
        self._exec("INSERT OR REPLACE INTO worker_transcripts VALUES (?,?)",
                   (exploration_id, self._blob_set(transcript)))

    def worker_transcript(self, exploration_id: str) -> Optional[str]:
        row = self._query_one(
            "SELECT transcript_blob FROM worker_transcripts WHERE exploration_id=?",
            (exploration_id,))
        return self._blob_get(row["transcript_blob"]) if row else None

    def runs_for_session(self, session_id: str) -> list[sqlite3.Row]:
        return self._query(
            "SELECT * FROM runs WHERE session_id=? ORDER BY created_at", (session_id,))

    def get_run(self, run_id: str) -> Optional[sqlite3.Row]:
        return self._query_one("SELECT * FROM runs WHERE id=?", (run_id,))

    def get_turn(self, turn_id: str) -> Optional[sqlite3.Row]:
        return self._query_one("SELECT * FROM turns WHERE id=?", (turn_id,))

    def latest_run(self, session_id: str) -> Optional[sqlite3.Row]:
        return self._query_one(
            "SELECT * FROM runs WHERE session_id=? ORDER BY created_at DESC LIMIT 1",
            (session_id,))

    def set_run_difficulty(self, run_id: str, difficulty: str, source: str,
                           n_workers: int) -> None:
        """Record the run's sizing. the difficulty MODEL is gone
        — values now record the fixed roster ("roster" / "fixed"); column
        names stay for schema stability."""
        self._exec(
            "UPDATE runs SET difficulty=?, difficulty_source=?, n_workers=? WHERE id=?",
            (difficulty, source, n_workers, run_id))

    def set_run_winner(self, run_id: str, proposal_id: str) -> None:
        self._exec("UPDATE runs SET winner_proposal_id=? WHERE id=?",
                   (proposal_id, run_id))

    def run_explorations(self, run_id: str) -> list[sqlite3.Row]:
        return self._query(
            "SELECT * FROM explorations WHERE run_id=? ORDER BY created_at",
            (run_id,))

    def latest_turn(self, session_id: str) -> Optional[sqlite3.Row]:
        return self._query_one(
            "SELECT * FROM turns WHERE session_id=? ORDER BY n DESC LIMIT 1", (session_id,)
        )

    def turns_for_session(self, session_id: str, limit: int = 20) -> list[sqlite3.Row]:
        return self._query(
            "SELECT * FROM turns WHERE session_id=? ORDER BY n DESC LIMIT ?", (session_id, limit)
        )

    def add_messages(self, session_id: str, turn_id: str, messages: list[dict]) -> None:
        with self._lock:
            for m in messages:
                mid = m.get("id") or f"msg-{uuid.uuid4().hex[:12]}"
                self._conn.execute(
                    "INSERT OR REPLACE INTO messages (id, session_id, turn_id, role, raw_blob, ts) VALUES (?,?,?,?,?,?)",
                    (mid, session_id, turn_id, m.get("role", "?"), self._blob_set(m), m.get("ts", time.time())),
                )
            self._conn.commit()

    def messages_for_turn(self, turn_id: str) -> list[dict]:
        rows = self._query("SELECT raw_blob FROM messages WHERE turn_id=? ORDER BY ts", (turn_id,))
        return [json.loads(self._blob_get(r["raw_blob"])) for r in rows]

    def user_text_for_session(self, session_id: str) -> list[str]:
        """All user-message text, in order. """
        rows = self._query(
            "SELECT raw_blob FROM messages WHERE session_id=? AND role='user' ORDER BY ts", (session_id,)
        )
        texts = []
        for r in rows:
            m = json.loads(self._blob_get(r["raw_blob"]))
            text = m.get("text") or m.get("content") or ""
            if isinstance(text, list):  # content blocks
                text = " ".join(b.get("text", "") for b in text if isinstance(b, dict))
            if text:
                texts.append(text)
        return texts

    def add_manual_edits(self, turn_id: str, edits: list[dict]) -> None:
        with self._lock:
            for e in edits:
                self._conn.execute(
                    "INSERT INTO manual_edits (turn_id, path, lines_added, lines_removed, hunks_blob) VALUES (?,?,?,?,?)",
                    (turn_id, e["path"], e.get("added", 0), e.get("removed", 0), self._blob_set(e.get("hunks", ""))),
                )
            self._conn.commit()

    def record_turn_files(self, turn_id: str, paths: list[str]) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM turn_files WHERE turn_id=?", (turn_id,))
            for p in paths:
                self._conn.execute("INSERT INTO turn_files (turn_id, path) VALUES (?,?)", (turn_id, p))
            self._conn.commit()

    def files_changed_recent(self, session_id: str, limit: int = 1) -> list[str]:
        """Paths changed in the most recent `limit` completed turns (deduped)."""
        turns = self._query(
            "SELECT id FROM turns WHERE session_id=? ORDER BY n DESC LIMIT ?",
            (session_id, limit),
        )
        if not turns:
            return []
        marks = ",".join("?" for _ in turns)
        rows = self._query(
            f"SELECT DISTINCT path FROM turn_files WHERE turn_id IN ({marks})",
            tuple(t["id"] for t in turns),
        )
        return [r["path"] for r in rows]

    def churn_files(self, session_id: str, window: int = 5, threshold: int = 3) -> list[str]:
        """Files edited in >= `threshold` of the last `window` turns."""
        turns = self._query(
            "SELECT id FROM turns WHERE session_id=? ORDER BY n DESC LIMIT ?", (session_id, window)
        )
        counts: dict[str, int] = {}
        for t in turns:
            rows = self._query("SELECT path FROM turn_files WHERE turn_id=?", (t["id"],))
            for r in rows:
                counts[r["path"]] = counts.get(r["path"], 0) + 1
        return [p for p, c in counts.items() if c >= threshold]

    # --------------------------------------------------------- explorations

    def create_exploration(
        self, session_id: str, strategy: str, harness: str, base_turn_id: str,
        ledger_version: int, budget: dict, run_id: str | None = None,
    ) -> str:
        ex_id = f"ex-{uuid.uuid4().hex[:10]}"
        self._exec(
            "INSERT INTO explorations (id, session_id, strategy, harness, base_turn_id, ledger_version, "
            "state, budget_json, run_id, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (ex_id, session_id, strategy, harness, base_turn_id, ledger_version,
             "PENDING", json.dumps(budget), run_id, time.time()),
        )
        self._record_transition(ex_id, None, "PENDING", "created")
        return ex_id

    def _record_transition(self, ex_id: str, frm: Optional[str], to: str, reason: str) -> None:
        self._exec(
            "INSERT INTO exploration_events (exploration_id, from_state, to_state, reason, ts) VALUES (?,?,?,?,?)",
            (ex_id, frm, to, reason, time.time()),
        )

    def exploration_state(self, ex_id: str) -> Optional[str]:
        row = self._query_one("SELECT state FROM explorations WHERE id=?", (ex_id,))
        return row["state"] if row else None

    def transition_exploration(self, ex_id: str, to_state: str, reason: str = "") -> None:
        row = self._query_one("SELECT state FROM explorations WHERE id=?", (ex_id,))
        if row is None:
            raise InvalidTransition(f"unknown exploration {ex_id}")
        frm = row["state"]
        if to_state not in ALLOWED_TRANSITIONS.get(frm, set()):
            raise InvalidTransition(f"illegal transition {frm} -> {to_state} for {ex_id} ({reason})")
        self._exec("UPDATE explorations SET state=? WHERE id=?", (to_state, ex_id))
        if to_state in TERMINAL:
            self._exec(
                "UPDATE explorations SET finished_at=? WHERE id=? AND finished_at IS NULL",
                (time.time(), ex_id),
            )
        self._record_transition(ex_id, frm, to_state, reason)

    def set_box_id(self, ex_id: str, box_id: str) -> None:
        self._exec("UPDATE explorations SET box_id=? WHERE id=?", (box_id, ex_id))

    def set_exploration_checkpoint(self, ex_id: str, checkpoint_id: str) -> None:
        """Resume checkpoint of a finished worker's box."""
        self._exec("UPDATE explorations SET checkpoint_id=? WHERE id=?",
                   (checkpoint_id, ex_id))

    def set_exit_reason(self, ex_id: str, exit_reason: str) -> None:
        self._exec("UPDATE explorations SET exit_reason=? WHERE id=?", (exit_reason, ex_id))

    def get_exploration(self, ex_id: str) -> Optional[sqlite3.Row]:
        return self._query_one("SELECT * FROM explorations WHERE id=?", (ex_id,))

    def live_explorations(self) -> list[sqlite3.Row]:
        marks = ",".join("?" for _ in NON_TERMINAL)
        return self._query(
            f"SELECT * FROM explorations WHERE state IN ({marks}) ORDER BY created_at",
            tuple(sorted(NON_TERMINAL)),
        )

    def all_exploration_box_ids(self) -> set[str]:
        """Every box id this instance ever recorded. The reaper's ownership
        boundary: an orchestrator may only reap boxes its OWN
        store remembers — account-wide sweeps let one leftover instance
        massacre every other instance's fleet ."""
        return {r["box_id"] for r in self._query(
            "SELECT DISTINCT box_id FROM explorations WHERE box_id IS NOT NULL")}

    def recent_explorations(self, session_id: str, since_ts: float) -> list[sqlite3.Row]:
        return self._query(
            "SELECT * FROM explorations WHERE session_id=? AND created_at>=? ORDER BY created_at DESC",
            (session_id, since_ts),
        )

    # ------------------------------------------------------------ proposals

    def save_proposal(
        self, exploration_id: str, patch: str, summary: dict, diff_stat: dict,
        preimages: dict[str, str] | None = None,
    ) -> str:
        p_id = f"H-{uuid.uuid4().hex[:6]}"
        payload = {"summary": summary, "diff_stat": diff_stat,
                   "preimages": preimages or {}}
        self._exec(
            "INSERT INTO proposals (id, exploration_id, patch_blob, summary_json, created_at) VALUES (?,?,?,?,?)",
            (p_id, exploration_id, self._blob_set(patch), self._blob_set(payload), time.time()),
        )
        return p_id

    def save_gate_result(
        self, proposal_id: str, stage: str, passed: bool, features: dict, reason: str
    ) -> None:
        self._exec(
            "INSERT INTO gate_results (proposal_id, stage, passed, features_json, reason, ts) VALUES (?,?,?,?,?,?)",
            (proposal_id, stage, int(passed), json.dumps(features), reason, time.time()),
        )

    def gate_features(self, proposal_id: str, stage: str) -> dict:
        """Features of the proposal's latest result at a stage ({} if none)."""
        row = self._query_one(
            "SELECT features_json FROM gate_results WHERE proposal_id=? AND "
            "stage=? ORDER BY ts DESC, rowid DESC LIMIT 1", (proposal_id, stage))
        return json.loads(row["features_json"] or "{}") if row else {}

    def get_proposal(self, p_id: str) -> Optional[dict]:
        row = self._query_one("SELECT * FROM proposals WHERE id=?", (p_id,))
        if row is None:
            return None
        payload = json.loads(self._blob_get(row["summary_json"]))
        return {
            "id": row["id"],
            "exploration_id": row["exploration_id"],
            "patch": self._blob_get(row["patch_blob"]),
            "rebased_onto": row["rebased_onto"],
            "rebase_status": row["rebase_status"],
            "qualified": bool(row["qualified"]) if row["qualified"] is not None else None,
            "regressions": json.loads(row["regressions_json"] or "[]"),
            "requirement_flips": json.loads(row["requirement_flips_json"] or "[]"),
            "summary": payload.get("summary"),
            "created_at": row["created_at"],
            "diff_stat": payload.get("diff_stat", {}),
            # {path: base64} baseline blobs for the --3way blob bridge
            "preimages": payload.get("preimages") or {},
        }

    def design_description(self, p_id: str) -> str:
        """What the engineers said about a design: the worker's own summary and the
        chairman's plan. Their words, so the delivery labels them as unverified."""
        p = self.get_proposal(p_id or "")
        if p is None:
            return ""
        parts = []
        summary = p["summary"]
        if isinstance(summary, dict):
            for key in ("summary", "addresses", "assumptions"):
                if summary.get(key):
                    parts.append(f"{key}: {json.dumps(summary[key], ensure_ascii=False)}")
        elif summary:
            parts.append(str(summary))
        ex = self.get_exploration(p["exploration_id"])
        chairman = self.get_run_chairman(ex["run_id"]) if ex and ex["run_id"] else None
        if chairman and chairman.get("directive"):
            parts.append(f"the plan it was built from: {str(chairman['directive'])[:6000]}")
        return "\n".join(parts)[:10_000]

    def set_proposal_rebase(self, p_id: str, onto: str, status: str) -> None:
        self._exec("UPDATE proposals SET rebased_onto=?, rebase_status=? WHERE id=?",
                   (onto, status, p_id))

    def set_proposal_selection(self, p_id: str, qualified: bool,
                               regressions: list, requirement_flips: list) -> None:
        self._exec(
            "UPDATE proposals SET qualified=?, regressions_json=?, "
            "requirement_flips_json=? WHERE id=?",
            (int(qualified), json.dumps(regressions),
             json.dumps(requirement_flips), p_id))

    def proposals_for_exploration(self, exploration_id: str) -> list[dict]:
        rows = self._query(
            "SELECT id FROM proposals WHERE exploration_id=? ORDER BY created_at",
            (exploration_id,))
        return [self.get_proposal(r["id"]) for r in rows]

    def run_proposals(self, run_id: str) -> list[dict]:
        """Proposals of a run's explorations, oldest first (selection order).
        Each carries its exploration's state so selection can skip the dead."""
        rows = self._query(
            "SELECT p.id, e.state AS exploration_state, "
            "e.strategy AS exploration_strategy FROM proposals p "
            "JOIN explorations e ON p.exploration_id=e.id "
            "WHERE e.run_id=? ORDER BY p.created_at", (run_id,))
        out = []
        for r in rows:
            p = self.get_proposal(r["id"])
            if p is not None:
                p["exploration_state"] = r["exploration_state"]
                p["exploration_strategy"] = r["exploration_strategy"]
                out.append(p)
        return out

    # The winning worker's box stays asleep after delivery so the main agent can
    # ask it questions. At most one per session.

    def keep_box(self, session_id: str, exploration_id: str, box_id: str) -> None:
        self.kv_set(f"kept_box:{session_id}", json.dumps(
            {"exploration_id": exploration_id, "box_id": box_id,
             "kept_at": time.time()}))

    def kept_box(self, session_id: str) -> Optional[dict]:
        raw = self.kv_get(f"kept_box:{session_id}")
        return json.loads(raw) if raw else None

    def clear_kept_box(self, session_id: str) -> None:
        self._exec("DELETE FROM kv WHERE key=?", (f"kept_box:{session_id}",))

    def kept_boxes(self) -> dict[str, dict]:
        rows = self._query("SELECT key, value FROM kv WHERE key LIKE 'kept_box:%'")
        return {r["key"].split(":", 1)[1]: json.loads(r["value"]) for r in rows}

    def run_pending_count(self, run_id: str) -> int:
        """Explorations of the run still in flight (worker not finished)."""
        row = self._query_one(
            "SELECT COUNT(*) AS n FROM explorations WHERE run_id=? AND state IN "
            "('PENDING','PROVISIONING','RUNNING','TESTING')", (run_id,))
        return int(row["n"]) if row else 0

    def run_workers_launched(self, run_id: str) -> int:
        row = self._query_one(
            "SELECT COUNT(*) AS n FROM explorations WHERE run_id=?", (run_id,))
        return int(row["n"]) if row else 0

    def proposals_in_states(self, states: Iterable[str]) -> list[dict]:
        marks = ",".join("?" for _ in states)
        rows = self._query(
            f"SELECT p.id FROM proposals p JOIN explorations e ON p.exploration_id=e.id "
            f"WHERE e.state IN ({marks}) ORDER BY p.created_at DESC",
            tuple(states),
        )
        return [self.get_proposal(r["id"]) for r in rows]

    # ----------------------------------------------------------- nudges etc

    def save_nudge(self, nudge_id: str, proposal_id: str, tier: str, text: str) -> None:
        self._exec(
            "INSERT INTO nudges (id, proposal_id, tier, text) VALUES (?,?,?,?)",
            (nudge_id, proposal_id, tier, text),
        )

    def mark_nudge_shown(self, nudge_id: str, channel: str) -> None:
        self._exec("UPDATE nudges SET shown_at=?, channel=? WHERE id=?",
                   (time.time(), channel, nudge_id))

    def pending_toast_nudges(self, limit: int = 20) -> list[sqlite3.Row]:
        """Nudges released to the plugin-toast channel but not yet
        displayed: shown_at set by the delivery engine, acked_at
        still NULL. session_id rides along so the toast can offer an
        'Open' action into the session the nudge belongs to."""
        return self._query(
            "SELECT n.id, n.proposal_id, n.tier, n.text, n.shown_at, "
            "e.session_id, e.strategy "
            "FROM nudges n "
            "JOIN proposals p ON n.proposal_id = p.id "
            "JOIN explorations e ON p.exploration_id = e.id "
            "WHERE n.shown_at IS NOT NULL AND n.acked_at IS NULL "
            "ORDER BY n.shown_at LIMIT ?", (limit,))

    def pending_agent_deliveries(self, session_id: str) -> list[dict]:
        """Released designs the main agent has not been handed yet, oldest
        first."""
        rows = self._query(
            "SELECT n.id, n.proposal_id, n.text, n.missed_json, p.patch_blob, "
            "e.base_turn_id "
            "FROM nudges n JOIN proposals p ON n.proposal_id = p.id "
            "JOIN explorations e ON p.exploration_id = e.id "
            "WHERE e.session_id=? AND n.shown_at IS NOT NULL "
            "AND n.agent_sent_at IS NULL ORDER BY n.shown_at", (session_id,))
        out = []
        latest = self.latest_turn(session_id)
        for r in rows:
            turn = self.get_turn(r["base_turn_id"] or "")
            out.append({"id": r["id"], "proposal_id": r["proposal_id"],
                        "text": r["text"], "patch": self._blob_get(r["patch_blob"]),
                        "basis_turn_n": turn["n"] if turn is not None else None,
                        "latest_turn_n": latest["n"] if latest is not None else None,
                        "description": self.design_description(r["proposal_id"]),
                        "missed": json.loads(r["missed_json"] or "[]")})
        return out

    def set_nudge_missed(self, nudge_id: str, messages: list[str]) -> None:
        self._exec("UPDATE nudges SET missed_json=? WHERE id=?",
                   (json.dumps(messages), nudge_id))

    def mark_agent_delivery_sent(self, nudge_id: str) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "UPDATE nudges SET agent_sent_at=? WHERE id=? "
                "AND agent_sent_at IS NULL", (time.time(), nudge_id))
            self._conn.commit()
            return cur.rowcount > 0

    def mark_nudge_acked(self, nudge_id: str, *, displayed: bool = True) -> bool:
        """The plugin displayed (or irrecoverably failed to display) the
        toast. Acked nudges are never redelivered — a missing toast
        surface (e.g. a headless run) would otherwise redeliver forever."""
        with self._lock:
            cur = self._conn.execute(
                "UPDATE nudges SET acked_at=?, ack_displayed=? WHERE id=? "
                "AND acked_at IS NULL",
                (time.time(), int(displayed), nudge_id))
            self._conn.commit()
            return cur.rowcount > 0

    # ------------------------------------------------------------- delivery

    def save_delivery(self, proposal_id: str, *, shown: bool,
                      channel: str = "", held_reason: str = "") -> str:
        d_id = f"dl-{uuid.uuid4().hex[:8]}"
        self._exec(
            "INSERT INTO deliveries (id, proposal_id, shown_at, channel, "
            "held_reason, ts) VALUES (?,?,?,?,?,?)",
            (d_id, proposal_id, time.time() if shown else None, channel,
             held_reason, time.time()))
        return d_id

    def runs_in_state(self, session_id: str, state: str) -> list[sqlite3.Row]:
        return self._query(
            "SELECT * FROM runs WHERE session_id=? AND state=? "
            "ORDER BY created_at", (session_id, state))

    def last_nudge_ts(self, session_id: str) -> Optional[float]:
        """Most recent shown nudge time for the session (rate limit)."""
        row = self._query_one(
            "SELECT MAX(n.shown_at) AS t FROM nudges n "
            "JOIN proposals p ON n.proposal_id=p.id "
            "JOIN explorations e ON p.exploration_id=e.id "
            "WHERE e.session_id=? AND n.shown_at IS NOT NULL", (session_id,))
        return row["t"] if row else None

    def nudges_shown_count(self, session_id: str) -> int:
        row = self._query_one(
            "SELECT COUNT(*) AS n FROM nudges n "
            "JOIN proposals p ON n.proposal_id=p.id "
            "JOIN explorations e ON p.exploration_id=e.id "
            "WHERE e.session_id=? AND n.shown_at IS NOT NULL", (session_id,))
        return int(row["n"]) if row else 0

    def latest_activity_ts(self, session_id: str) -> Optional[float]:
        """Most recent user activity: latest message or completed turn."""
        row = self._query_one(
            "SELECT MAX(t) AS t FROM ("
            "SELECT MAX(m.ts) AS t FROM messages m WHERE m.session_id=? "
            "UNION ALL SELECT MAX(tu.completed_at) AS t FROM turns tu "
            "WHERE tu.session_id=?)", (session_id, session_id))
        return row["t"] if row else None

    # ------------------------------------------------------------ dismissal

    def save_dismissal(self, session_id: str, fingerprint: dict,
                       reason: str) -> None:
        self._exec(
            "INSERT INTO dismissals (id, session_id, fingerprint, reason, ts) "
            "VALUES (?,?,?,?,?)",
            (f"dm-{uuid.uuid4().hex[:8]}", session_id,
             json.dumps(fingerprint, sort_keys=True), reason, time.time()))

    def dismissal_matches(self, session_id: str, fingerprint: dict) -> bool:
        row = self._query_one(
            "SELECT 1 AS x FROM dismissals WHERE session_id=? AND fingerprint=? "
            "LIMIT 1", (session_id, json.dumps(fingerprint, sort_keys=True)))
        return row is not None

    def dismissal_reasons(self, session_id: str) -> list[str]:
        return [r["reason"] for r in self._query(
            "SELECT reason FROM dismissals WHERE session_id=?", (session_id,))]

    # ------------------------------------------------------------- adoption

    def save_adoption(self, ad_id: str, proposal_id: str, session_id: str,
                      backup_dir: str, files: list[str],
                      created_files: list[str], test_command: str) -> None:
        self._exec(
            "INSERT INTO adoptions (id, proposal_id, session_id, backup_dir, "
            "files_json, created_files_json, test_command, applied_at, "
            "created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (ad_id, proposal_id, session_id, backup_dir,
             json.dumps(files), json.dumps(created_files), test_command,
             time.time(), time.time()))

    def set_adoption_tests(self, ad_id: str, passed: bool,
                           tests_json: str) -> None:
        self._exec("UPDATE adoptions SET test_passed=?, tests_json=? WHERE id=?",
                   (int(passed) if passed is not None else None,
                    tests_json, ad_id))

    def mark_adoption_notified(self, ad_id: str, channel: str) -> None:
        self._exec("UPDATE adoptions SET notified=1, notify_channel=? WHERE id=?",
                   (channel, ad_id))

    def mark_adoption_undone(self, ad_id: str) -> None:
        self._exec("UPDATE adoptions SET undone_at=? WHERE id=?",
                   (time.time(), ad_id))

    def get_adoption(self, ad_id: str) -> Optional[sqlite3.Row]:
        return self._query_one("SELECT * FROM adoptions WHERE id=?", (ad_id,))

    def latest_adoption(self, session_id: str | None = None) -> Optional[sqlite3.Row]:
        """Most recent adoption, optionally scoped to a session; undone
        adoptions still count as 'latest' (undo of an undone one is a no-op)."""
        if session_id is None:
            return self._query_one(
                "SELECT * FROM adoptions ORDER BY created_at DESC LIMIT 1")
        return self._query_one(
            "SELECT * FROM adoptions WHERE session_id=? "
            "ORDER BY created_at DESC LIMIT 1", (session_id,))

    def adoptions_list(self, session_id: str | None = None) -> list[sqlite3.Row]:
        if session_id is None:
            return self._query(
                "SELECT * FROM adoptions ORDER BY created_at DESC")
        return self._query(
            "SELECT * FROM adoptions WHERE session_id=? ORDER BY created_at DESC",
            (session_id,))

    def record_user_action(self, target: str, action: str, reason: str = "", via: str = "cli") -> None:
        self._exec(
            "INSERT INTO user_actions (target, action, reason, via, ts) VALUES (?,?,?,?,?)",
            (target, action, reason, via, time.time()),
        )

    # --------------------------------------------------------------- costs

    def record_cost(self, entry: CostEntry) -> None:
        self._exec(
            "INSERT INTO costs (ref, kind, model, tier, input_tokens, output_tokens, box_seconds, usd, ts) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (entry.ref, entry.kind, entry.model, entry.tier, entry.input_tokens,
             entry.output_tokens, entry.box_seconds, entry.usd, time.time()),
        )

    def spend_since(self, since_ts: float) -> float:
        row = self._query_one("SELECT COALESCE(SUM(usd),0) AS s FROM costs WHERE ts>=?", (since_ts,))
        return row["s"] if row else 0.0

    def spend_by_ref(self, ref: str) -> float:
        row = self._query_one("SELECT COALESCE(SUM(usd),0) AS s FROM costs WHERE ref=?", (ref,))
        return row["s"] if row else 0.0

    # ----------------------------------------------------------------- envs

    def get_env(self, key: str) -> Optional[sqlite3.Row]:
        return self._query_one("SELECT * FROM envs WHERE key=?", (key,))

    def save_env(self, key: str, checkpoint_id: str, expires_at: float, setup: dict, flaky: list) -> None:
        self._exec(
            "INSERT OR REPLACE INTO envs (key, checkpoint_id, expires_at, setup_json, flaky_tests_json) "
            "VALUES (?,?,?,?,?)",
            (key, checkpoint_id, expires_at, json.dumps(setup), json.dumps(flaky)),
        )

    # ------------------------------------------------------------------- kv

    def kv_set(self, key: str, value: str) -> None:
        self._exec("INSERT OR REPLACE INTO kv (key, value) VALUES (?,?)", (key, value))

    def kv_get(self, key: str) -> Optional[str]:
        row = self._query_one("SELECT value FROM kv WHERE key=?", (key,))
        return row["value"] if row else None

    def kv_add_to_set(self, key: str, member: str) -> bool:
        """Add member to a set stored as JSON; returns False if already present."""
        with self._lock:
            raw = self.kv_get(key)
            members = json.loads(raw) if raw else []
            if member in members:
                return False
            members.append(member)
            self.kv_set(key, json.dumps(members))
            return True
