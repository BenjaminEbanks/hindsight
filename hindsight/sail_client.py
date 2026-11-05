"""Thin wrapper over the Sail SDK. All Sail calls go through here (Appendix C).

Verified against sail-sdk 0.17.2 by inspection (see docs/VERIFY.md):
  - Sailbox.create / exec / run / fs.write_files / fs.read / fs.upload_file /
    fs.download_file / checkpoint(ttl_seconds=) / from_checkpoint / terminate / list
  - No Sailbox.fork exists; "forking" = checkpoint a running box, then
    from_checkpoint twice (background processes continue, exec'd commands stop).
  - sail.inference.chat.completions.create(**payload, headers=...) supports
    custom headers (e.g. X-SailboxId).
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional

import sail
from sail import App, EgressPolicy, Sailbox

from .config import DEFAULT_APP_NAME


log = logging.getLogger(__name__)

_BOX_LIMIT_TEXTS = ("at its limit", "limits are refreshing")
_BOX_SLOT_WAIT_SECONDS = 90 * 60.0
_BOX_SLOT_RETRY_SECONDS = 30.0
_WRITE_ATTEMPTS = 3
_WRITE_RETRY_SECONDS = 5.0


class SailClientError(Exception):
    pass


@dataclass
class ExecOut:
    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool


@dataclass
class BoxInfo:
    id: str
    name: str
    state: str


@dataclass
class BoxHandle:
    id: str
    name: str
    created_at: float = field(default_factory=time.time)


class SailClient:
    """Synchronous wrapper. The orchestrator calls this from worker threads."""

    def __init__(self, app_name: str = DEFAULT_APP_NAME, box_policy: str | None = None,
                 box_size: str | None = None):
        self.app_name = app_name
        # A box reserves its size's vCPUs whether it uses them or not, and the
        # organization's limit counts reservations. A box from a checkpoint keeps
        # its source's size.
        self.box_size = box_size
        # A saved egress policy every box gets, so a box can make model calls
        # without holding the key. A box is never left without a requested policy.
        self.box_policy = box_policy
        self._app: App | None = None

    @property
    def app(self) -> App:
        if self._app is None:
            self._app = App.find(self.app_name, mint_if_missing=True)
        return self._app

    def _box(self, box_id: str) -> Sailbox:
        return Sailbox.from_id(box_id)

    # ------------------------------------------------------------- lifecycle

    def _saved_policy(self) -> EgressPolicy:
        """Sail fetches policies by id, so the configured name is looked up."""
        for policy in EgressPolicy.list():
            if policy.name == self.box_policy:
                return policy
        raise SailClientError(f"no saved egress policy named {self.box_policy}")

    def create_box(
        self, name: str, *, image: Any = None, from_checkpoint_id: str | None = None,
        max_lifetime_seconds: int | None = None, size: Any = None,
    ) -> BoxHandle:
        try:
            policy = self._saved_policy() if self.box_policy else None
            if from_checkpoint_id is not None:
                box = self._when_a_slot_is_free(
                    lambda: Sailbox.from_checkpoint(from_checkpoint_id, name=name), name)
                if policy is not None:
                    try:
                        box.set_egress_policy(policy)
                    except Exception:
                        box.terminate()
                        raise
            else:
                kwargs: dict[str, Any] = dict(
                    app=self.app, name=name, image=image, visibility="private"
                )
                if max_lifetime_seconds:
                    kwargs["max_lifetime_seconds"] = max_lifetime_seconds
                size = size if size is not None else self.box_size
                if size is not None:
                    kwargs["size"] = size
                if policy is not None:
                    kwargs["egress_policy"] = policy
                box = self._when_a_slot_is_free(lambda: Sailbox.create(**kwargs), name)
            return BoxHandle(id=box.sailbox_id, name=name)
        except Exception as e:  # keep the orchestrator alive; caller decides retry
            raise SailClientError(f"create_box({name}) failed: {e}") from e

    @staticmethod
    def _when_a_slot_is_free(create, name: str):
        """The organization's limits on live boxes and reserved vCPUs are a queue, not
        a failure: losing a worker to them would change the experiment. Gives up after
        the wait."""
        deadline = time.monotonic() + _BOX_SLOT_WAIT_SECONDS
        while True:
            try:
                return create()
            except Exception as e:  # noqa: BLE001 - the SDK has no type for this one
                if (not any(text in str(e) for text in _BOX_LIMIT_TEXTS)
                        or time.monotonic() >= deadline):
                    raise
                log.warning("at a box limit; waiting for a slot for %s", name)
                time.sleep(_BOX_SLOT_RETRY_SECONDS)

    def from_checkpoint(self, checkpoint_id: str, name: str) -> BoxHandle:
        """Fork path: new running box from a durable checkpoint."""
        try:
            box = self._when_a_slot_is_free(
                lambda: Sailbox.from_checkpoint(checkpoint_id, name=name), name)
            return BoxHandle(id=box.sailbox_id, name=name)
        except Exception as e:
            raise SailClientError(f"from_checkpoint({checkpoint_id}) failed: {e}") from e

    def terminate(self, box_id: str) -> None:
        try:
            self._box(box_id).terminate()  # idempotent per SDK docs
        except Exception as e:
            raise SailClientError(f"terminate({box_id}) failed: {e}") from e

    def sleep(self, box_id: str) -> None:
        """Put the box to sleep; its files survive."""
        try:
            self._box(box_id).sleep()
        except Exception as e:
            raise SailClientError(f"sleep({box_id}) failed: {e}") from e

    def wake(self, box_id: str) -> None:
        """Wake a sleeping box (takes a few seconds)."""
        try:
            self._box(box_id).resume()
        except Exception as e:
            raise SailClientError(f"wake({box_id}) failed: {e}") from e

    def checkpoint(self, box_id: str, ttl_seconds: int | None = None, name: str | None = None) -> str:
        try:
            cp = self._box(box_id).checkpoint(ttl_seconds=ttl_seconds, name=name)
            return cp.checkpoint_id
        except Exception as e:
            raise SailClientError(f"checkpoint({box_id}) failed: {e}") from e

    def list_boxes(self, search: str | None = None) -> list[BoxInfo]:
        try:
            kwargs: dict[str, Any] = {}
            if search:
                kwargs["search"] = search
            boxes = Sailbox.list(**kwargs)
            out = []
            for b in boxes:
                out.append(BoxInfo(id=b.sailbox_id, name=b.name, state=str(b.status)))
            return out
        except Exception as e:
            raise SailClientError(f"list_boxes failed: {e}") from e

    # ------------------------------------------------------------------- exec

    def run(self, box_id: str, command: list[str] | str, *, timeout: float | None = None,
            cwd: str | None = None, env: Mapping[str, str] | None = None) -> ExecOut:
        try:
            r = self._box(box_id).run(command, timeout=timeout, cwd=cwd, env=dict(env) if env else None)
            return ExecOut(
                exit_code=r.exit_code,
                stdout=r.stdout if isinstance(r.stdout, str) else r.stdout.decode(errors="replace"),
                stderr=r.stderr if isinstance(r.stderr, str) else r.stderr.decode(errors="replace"),
                timed_out=bool(r.timed_out),
            )
        except Exception as e:
            raise SailClientError(f"run({box_id}, {command!r}) failed: {e}") from e

    def start_background(self, box_id: str, command: list[str] | str, *, cwd: str | None = None,
                          env: Mapping[str, str] | None = None) -> None:
        """Start a long-running process and return immediately.

        The SDK accepts `list[str] | str` in the signature but RAISES
        `InvalidArgumentError("background requires a shell command string")`
        for the list form (sailbox.py, sail-sdk 0.17.2). Worse, the failure
        surface is per-worker records, not an eval-stopper: one
        live eval ran 22 episodes / 42 explorations with a 100%-failed fleet
        before a swarm probe surfaced it. Join lists here
        at the single choke point so every caller is safe.
        """
        if not isinstance(command, str):
            import shlex
            command = shlex.join(command)
        try:
            proc = self._box(box_id).exec(command, background=True, cwd=cwd, env=dict(env) if env else None)
            proc.close()
        except Exception as e:
            raise SailClientError(f"start_background({box_id}) failed: {e}") from e

    # --------------------------------------------------------------------- fs

    def write_files(self, box_id: str, files: Mapping[str, str | bytes]) -> None:
        """Rewriting a file is safe, so a dropped connection is retried: one reset here
        ended 3 of 4 benchmark tasks and lost a finished design."""
        from .models import _is_transient  # models imports this module
        for attempt in range(_WRITE_ATTEMPTS):
            try:
                self._box(box_id).fs.write_files(dict(files))
                return
            except Exception as e:
                if attempt == _WRITE_ATTEMPTS - 1 or not _is_transient(e):
                    raise SailClientError(f"write_files({box_id}) failed: {e}") from e
                time.sleep(_WRITE_RETRY_SECONDS * (attempt + 1))

    def read_file(self, box_id: str, path: str) -> bytes:
        try:
            return self._box(box_id).fs.read(path)
        except Exception as e:
            raise SailClientError(f"read_file({box_id}, {path}) failed: {e}") from e

    def list_dir(self, box_id: str, path: str) -> list[str]:
        """Immediate entry names in a box directory ([] if it doesn't exist).
        Verified against sail-sdk 0.17.2: fs.ls raises on a missing path, so
        the ENOENT-style case is mapped to [] here. Best-effort by design:
        the Q&A poll treats a failed listing as 'no questions'."""
        try:
            return [e.name for e in self._box(box_id).fs.ls(path)]
        except Exception:
            return []

    def upload_file(self, box_id: str, local_path: str, guest_path: str) -> None:
        try:
            self._box(box_id).fs.upload_file(local_path, guest_path)
        except Exception as e:
            raise SailClientError(f"upload_file({box_id}, {guest_path}) failed: {e}") from e

    def download_file(self, box_id: str, guest_path: str, local_path: str) -> None:
        try:
            self._box(box_id).fs.download_file(guest_path, local_path)
        except Exception as e:
            raise SailClientError(f"download_file({box_id}, {guest_path}) failed: {e}") from e

    def file_exists(self, box_id: str, path: str) -> bool:
        try:
            return bool(self._box(box_id).fs.exists(path))
        except Exception as e:
            raise SailClientError(f"exists({box_id}, {path}) failed: {e}") from e

    # -------------------------------------------------------------- inference

    def inference_chat(
        self, model: str, messages: list[dict], *, extra_headers: Mapping[str, str] | None = None,
        timeout: float = 300.0, **payload: Any,
    ) -> dict:
        """timeout: a hung inference connection otherwise
        wedges the event-pipeline thread forever — the episode sits
        'waiting for background' until the box cap kills it (found live
        on the relay probe: thread stuck in do_epoll_wait on an
        ESTABLISHED api connection for 25+ min with every state at
        rest). A timed-out call raises, matches the transient markers,
        and the model_fn retry path handles it."""
        try:
            return sail.inference.chat.completions.create(
                model=model, messages=messages,
                headers=dict(extra_headers) if extra_headers else None,
                timeout=timeout,
                **payload,
            )
        except Exception as e:
            raise SailClientError(f"inference_chat({model}) failed: {e}") from e
