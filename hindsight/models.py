"""Model routing: the coordinator's model calls, through Sail inference or any
OpenAI-compatible provider. Every call is cost-recorded.
"""

from __future__ import annotations

import logging
import os
import time
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Callable

import httpx

from .budget import BudgetManager
from .prompts import TEMPLATE_VERSIONS
from .config import ModelsConfig
from .sail_client import SailClient
from .store import Store

# Transient inference failures worth retrying (observed live:
# "request timed out: error sending request ... operation timed out"
# killed the only gate firing in the whole eval run). Everything else
# (auth, bad request, unknown model) raises immediately.
# "chat completion failed" added — the SDK's generic wrapper
# text for inference saturation; killed 6/18 concurrent treatments in the
# 36-episode table (model calls exhaust, episodes stall to timeout).
# The wrapper flattens the underlying cause, so match the wrapper itself.
_TRANSIENT_MARKERS = (
    "timed out", "timeout", "connection reset", "connection refused",
    "connection closed", "temporarily unavailable", "rate limit",
    "overloaded", "bad gateway", "service unavailable", "internal error",
    "chat completion failed",
)

log = logging.getLogger(__name__)


def _is_transient(err: BaseException) -> bool:
    status = getattr(err, "status", None)
    if isinstance(status, int) and 500 <= status < 600:
        return True
    text = str(err).lower()
    return any(m in text for m in _TRANSIENT_MARKERS)


class ModelCallError(RuntimeError):
    """A failed call to the provider. `status` is the HTTP status, when there was one."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class OpenAICompatibleClient:
    """Chat completions from any provider that speaks the OpenAI format."""

    def __init__(self, base_url: str, api_key: str, *, max_tokens_param: str = "max_tokens"):
        self.url = f"{base_url.rstrip('/')}/chat/completions"
        self._api_key = api_key
        self.max_tokens_param = max_tokens_param

    def inference_chat(self, model: str, messages: list[dict], *, timeout: float = 300.0,
                       max_output_tokens: int | None = None, **payload: Any) -> dict:
        body = {"model": model, "messages": messages, **payload}
        if max_output_tokens is not None:
            body[self.max_tokens_param] = max_output_tokens
        try:
            r = httpx.post(self.url, json=body, timeout=timeout,
                           headers={"Authorization": f"Bearer {self._api_key}"})
        except httpx.TimeoutException as e:
            raise ModelCallError(f"{model}: request timed out") from e
        except httpx.TransportError as e:
            raise ModelCallError(f"{model}: connection closed ({type(e).__name__})") from e
        if r.status_code == 429:
            raise ModelCallError(f"{model}: rate limit (HTTP 429)", status=429)
        if r.status_code >= 400:
            raise ModelCallError(f"{model}: HTTP {r.status_code}: {r.text[:500]}",
                                 status=r.status_code)
        return r.json()


def make_chat_client(models: ModelsConfig, sail: SailClient):
    """Sail inference by default, otherwise the configured provider."""
    if models.uses_sail_inference:
        return sail
    key = os.environ.get(models.api_key_env)
    if not key:
        raise ValueError(f"models.base_url is {models.base_url}, "
                         f"but {models.api_key_env} is not set")
    return OpenAICompatibleClient(models.base_url, key,
                                  max_tokens_param=models.max_tokens_param)


def make_model_fn(
    chat, model: str, budget: BudgetManager, *, tier: str = "standard",
    ref_prefix: str = "model", max_output_tokens: int = 4096,
    retries: int = 2, retry_delay_seconds: float = 2.0, timeout_seconds: float = 720.0,
) -> Callable[[str], str]:
    """Return a sync prompt->completion-text function with cost recording.

    Transient inference failures (timeouts, 5xx, rate limits) are retried
    with linear backoff; permanent errors raise on the first attempt.

    The timeout is long because reasoning models can be slow without being stuck:
    GLM-5.3-Flash took 450-470s on a 30k-character critic prompt, writing 30k tokens
    whatever max_output_tokens said, so a 300s limit failed every try."""

    def fn(prompt: str) -> str:
        resp = None
        for attempt in range(retries + 1):
            try:
                resp = chat.inference_chat(
                    model, [{"role": "user", "content": prompt}],
                    max_output_tokens=max_output_tokens, timeout=timeout_seconds,
                )
                break
            except Exception as e:  # noqa: BLE001 - retried or re-raised below
                if attempt == retries or not _is_transient(e):
                    raise
                time.sleep(retry_delay_seconds * (attempt + 1))
        assert resp is not None
        usage = resp.get("usage", {}) or {}
        budget.record_model_cost(
            ref=f"{ref_prefix}-{model}", model=model,
            input_tokens=int(usage.get("prompt_tokens", usage.get("input_tokens", 0)) or 0),
            output_tokens=int(usage.get("completion_tokens", usage.get("output_tokens", 0)) or 0),
            tier=tier,
        )
        choices = resp.get("choices", [])
        if choices:
            msg = choices[0].get("message", {})
            content = msg.get("content")
            if isinstance(content, str):
                return content
            if isinstance(content, list):  # content blocks
                return "".join(b.get("text", "") for b in content if isinstance(b, dict))
        return ""

    return fn


_call_context: ContextVar[dict] = ContextVar("hindsight_call_context", default={})


@contextmanager
def call_context(**fields: str):
    """What the model calls inside this block belong to, for the trace."""
    token = _call_context.set({**_call_context.get(), **fields})
    try:
        yield
    finally:
        _call_context.reset(token)


def recorded(store: Store, model: str, step: str,
             fn: Callable[[str], str]) -> Callable[[str], str]:
    """Keep every prompt and reply. When a model makes a wrong call, this is what
    you need to read, and it cannot be got back afterwards. `step` names the
    prompt template, and call_context can narrow it (the judge has two)."""

    def wrapped(prompt: str) -> str:
        ctx = _call_context.get()
        name = ctx.get("step", step)
        t0 = time.time()
        response, error = "", ""
        call_id = None
        try:  # recorded now, so a call that hangs shows in the trace
            call_id = store.save_model_call(
                session_id=ctx.get("session_id", ""), run_id=ctx.get("run_id", ""),
                step=name, model=model,
                prompt_version=TEMPLATE_VERSIONS.get(name, "?"), prompt=prompt,
                response="", error="", seconds=-1.0)
        except Exception as e:  # noqa: BLE001 - never lose the answer over its record
            log.warning("model call not recorded: %s", str(e)[:200])
        try:
            response = fn(prompt) or ""
            return response
        except Exception as e:
            error = f"{type(e).__name__}: {e}"
            raise
        finally:
            try:
                if call_id is not None:
                    store.finish_model_call(call_id, response=response, error=error[:2000],
                                            seconds=time.time() - t0)
            except Exception as e:  # noqa: BLE001 - never lose the answer over its record
                log.warning("model call not recorded: %s", str(e)[:200])

    return wrapped
