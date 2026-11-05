"""Box reaper: kill leaked boxes before they burn credits.

Ownership boundary: an orchestrator reaps ONLY
boxes its own store remembers (explorations.box_id) minus the live ones.
Box names carrying hs-<id> never amounted to an ownership CHECK — until
this change any instance swept every hs-* box on the account, and one
leftover `hindsight up` spent an evening killing swarm probe boxes
(misread for hours as a platform host-loss flap). Multiple concurrent
orchestrators (swarm eval boxes, parallel sessions) make account-wide
sweeps unworkable by construction.
"""

from __future__ import annotations

import threading
import time
from typing import Callable

from .sail_client import SailClient
from .store import Store

REAP_INTERVAL_S = 300


class Reaper:
    def __init__(self, sail: SailClient, store: Store, on_incident: Callable[[str, float], None] | None = None,
                 on_tick: Callable[[], None] | None = None):
        self.sail = sail
        self.store = store
        self.on_incident = on_incident
        self.on_tick = on_tick
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name="hindsight-reaper", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.wait(REAP_INTERVAL_S):
            try:
                if self.on_tick:
                    self.on_tick()
                self.reap_once()
            except Exception as e:  # noqa: BLE001 - the reaper must never die
                if self.on_incident:
                    self.on_incident(f"reaper error: {e}", 0.0)

    def reap_once(self) -> list[str]:
        """Terminate boxes THIS instance created that are no longer owned by
        a live exploration. Returns the ids terminated.

        The candidate set is the store's known box ids minus the live
        exploration ids — never the account-wide box listing. Already-
        reaped ids are remembered in-process so sweeps stay quiet."""
        live_ids = {row["box_id"] for row in self.store.live_explorations() if row["box_id"]}
        # A winner's box asleep for questions is owned too, until released.
        live_ids |= {k["box_id"] for k in self.store.kept_boxes().values()}
        known_ids = self.store.all_exploration_box_ids() - getattr(self, "_reaped", set())
        terminated: list[str] = []
        for box_id in sorted(known_ids - live_ids):
            try:
                self.sail.terminate(box_id)
                terminated.append(box_id)
                if self.on_incident:
                    self.on_incident(f"reaped leaked box {box_id}", 0.0)
            except Exception:
                # Box already gone or a transient API failure; either way
                # the store still remembers it, so mark it reaped to keep
                # future sweeps quiet (terminate is idempotent server-side).
                pass
            self._reaped = getattr(self, "_reaped", set()) | {box_id}
        return terminated
