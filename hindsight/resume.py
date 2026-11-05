"""The main agent's questions for the finished worker.

The user only talks to the main agent. When it needs more than the stored facts,
`hindsight_ask` goes to the run's winner: the final worker, or the lone winning
worker if none ran. That worker's box sleeps after delivery; asking wakes it,
continues the same agent conversation, and puts it back to sleep, so it remembers
its work.

The box is terminated on adopt, dismiss, a newer delivery for that session, session
end, or after `experiments.winner_box_ttl_hours`. If waking fails, the worker is
revived from the checkpoint taken when it finished. If that fails too we say so:
an answer is never fabricated.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import Optional

from .config import HindsightConfig
from .sail_client import SailClient, SailClientError
from .store import CostEntry, Store

log = logging.getLogger("hindsight")

_ANSWER_TIMEOUT_SECONDS = 300.0
_POLL_SECONDS = 5.0


def agent_session_of(exploration) -> str:
    """The agent conversation a worker runs in. A follow-up round keeps its
    predecessor's, so the worker remembers its work."""
    try:
        budget = json.loads(exploration["budget_json"] or "{}")
    except ValueError:
        budget = {}
    return (budget.get("agent_session")
            or f"ses-hs-worker-{exploration['id']}"[:60])


class WorkerResume:
    def __init__(self, cfg: HindsightConfig, store: Store, sail: SailClient):
        self.cfg = cfg
        self.store = store
        self.sail = sail
        self._lock = threading.Lock()  # one question at a time per box

    # ------------------------------------------------------------- keeping

    def keep(self, exploration_id: str) -> None:
        """The run's result was sent: its worker stays asleep for questions, and
        the session's previous one is done."""
        row = self.store.get_exploration(exploration_id)
        if row is None or not row["box_id"]:
            return
        kept = self.store.kept_box(row["session_id"])
        if kept is not None and kept["box_id"] != row["box_id"]:
            self._terminate(kept["box_id"])
        self.store.keep_box(row["session_id"], exploration_id, row["box_id"])

    def release_proposal(self, proposal_id: str) -> None:
        """Adopted or dismissed: its worker has nothing left to answer."""
        row = self._worker_for(proposal_id)
        if row is None:
            return
        kept = self.store.kept_box(row["session_id"])
        if kept is not None and kept["exploration_id"] == row["id"]:
            self.release_session(row["session_id"])

    def release_session(self, session_id: str) -> None:
        kept = self.store.kept_box(session_id)
        if kept is not None:
            self._terminate(kept["box_id"])
            self.store.clear_kept_box(session_id)

    def expire(self, now: Optional[float] = None) -> None:
        ttl = self.cfg.experiments.winner_box_ttl_hours * 3600
        now = time.time() if now is None else now
        for session_id, kept in self.store.kept_boxes().items():
            if now - kept["kept_at"] > ttl:
                self.release_session(session_id)

    def _terminate(self, box_id: str) -> None:
        try:
            self.sail.terminate(box_id)
        except SailClientError:
            log.warning("could not terminate %s", box_id)

    # -------------------------------------------------------------- asking

    def can_resume(self, proposal_id: str) -> bool:
        row = self._worker_for(proposal_id)
        if row is None:
            return False
        kept = self.store.kept_box(row["session_id"])
        return bool(row["checkpoint_id"]) or (
            kept is not None and kept["exploration_id"] == row["id"])

    def answer(self, proposal_id: str, question: str,
               timeout: float = _ANSWER_TIMEOUT_SECONDS) -> dict:
        """Returns {question, answer, source, exit_reason?, error?}. Never raises:
        an unavailable worker is reported as source="unavailable"."""
        row = self._worker_for(proposal_id)
        if row is None:
            return {"question": question, "answer": None, "source": "unavailable",
                    "error": "unknown proposal or exploration"}
        with self._lock:
            kept = self.store.kept_box(row["session_id"])
            if kept is not None and kept["exploration_id"] == row["id"]:
                out = self._ask_kept(row, kept["box_id"], question, timeout)
                if out is not None:
                    return out
            return self._ask_from_checkpoint(row, question, timeout)

    def _ask_kept(self, row, box_id: str, question: str,
                  timeout: float) -> Optional[dict]:
        """None when the box cannot be woken: the caller falls back."""
        t0 = time.time()
        try:
            self.sail.wake(box_id)
        except SailClientError as e:
            log.warning("could not wake %s: %s", box_id, e)
            self.release_session(row["session_id"])
            return None
        out = self._run_answer(row, box_id, question, timeout)
        self._record_box_cost(row, t0)
        if out.get("source") == "worker_resume":
            try:
                self.sail.sleep(box_id)
                return out
            except SailClientError:
                log.warning("could not put %s back to sleep", box_id)
        # Asleep is the only state a kept box may be left in.
        self.release_session(row["session_id"])
        return out

    def _ask_from_checkpoint(self, row, question: str, timeout: float) -> dict:
        if not row["checkpoint_id"]:
            return {"question": question, "answer": None, "source": "unavailable",
                    "error": "no resume checkpoint for this worker"}
        t0 = time.time()
        try:
            box = self.sail.create_box(f"hs-answer-{row['id'][:12]}",
                                       from_checkpoint_id=row["checkpoint_id"])
        except SailClientError as e:
            # Most likely a TTL-expired checkpoint.
            return {"question": question, "answer": None, "source": "unavailable",
                    "error": f"checkpoint unusable: {e}"}
        try:
            return self._run_answer(row, box.id, question, timeout)
        finally:
            try:
                self.sail.terminate(box.id)
            except SailClientError:
                log.warning("could not terminate answer box %s", box.id)
            self._record_box_cost(row, t0)

    def _run_answer(self, row, box_id: str, question: str, timeout: float) -> dict:
        task = {
            "mode": "answer",
            "exploration_id": row["id"],
            "agent_session": agent_session_of(row),
            "question": question,
            "model": f"{self.cfg.models.provider}/{self.cfg.models.worker}",
            "tier": "flex",
            "harness": row["harness"] or "opencode",
            "budget": {"max_wall_clock_s": int(timeout)},
        }
        driver_src = Path(__file__).parent / "box" / "driver.py"
        try:
            self.sail.run(box_id, ["bash", "-c", "rm -f /work/answer.json"])
            self.sail.write_files(box_id, {
                "/work/task.json": json.dumps(task),
                "/work/driver.py": driver_src.read_text(),
            })
            self.sail.start_background(box_id, [
                "bash", "-c",
                "cd /work && nohup python3 driver.py >> /work/driver.log 2>&1 & echo started"])
            deadline = time.time() + timeout
            while time.time() < deadline:
                time.sleep(_POLL_SECONDS)
                if self.sail.file_exists(box_id, "/work/answer.json"):
                    raw = json.loads(self.sail.read_file(box_id, "/work/answer.json"))
                    raw["source"] = "worker_resume"
                    return raw
            return {"question": question, "answer": None, "source": "timeout",
                    "error": "worker did not answer in time"}
        except SailClientError as e:
            return {"question": question, "answer": None, "source": "error",
                    "error": str(e)}

    # ------------------------------------------------------------ internals

    def _record_box_cost(self, row, t0: float) -> None:
        elapsed = time.time() - t0
        self.store.record_cost(CostEntry(
            ref=row["id"], kind="box", box_seconds=elapsed,
            usd=0.002 * elapsed / 60))

    def _worker_for(self, proposal_id: str):
        """The worker that answers for a proposal: its run's winner when there is
        one (a send-back can make the delivered proposal the newer of two)."""
        p = self.store.get_proposal(proposal_id)
        if p is None:
            return None
        row = self.store.get_exploration(p["exploration_id"])
        if row is None or not row["run_id"]:
            return row
        run = self.store.get_run(row["run_id"])
        winner = (self.store.get_proposal(run["winner_proposal_id"])
                  if run is not None and run["winner_proposal_id"] else None)
        return (self.store.get_exploration(winner["exploration_id"])
                if winner is not None else row)
