"""Using your own model provider. The provider here is a local fake HTTP server."""

import json
import threading
import tomllib
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest
from pydantic import BaseModel

from hindsight.box_key import policy_document
from hindsight.budget import BudgetManager
from hindsight.config import HindsightConfig, ModelsConfig
from hindsight.models import (ModelCallError, OpenAICompatibleClient, _is_transient,
                              make_chat_client, make_model_fn)

REPO = Path(__file__).parents[2]
KEY = "fake-key-for-this-test"


class FakeBudget:
    def __init__(self):
        self.calls = []

    def record_model_cost(self, **kw):
        self.calls.append(kw)


@pytest.fixture
def provider():
    seen, replies = [], []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["content-length"])))
            seen.append({"path": self.path, "auth": self.headers["authorization"], "body": body})
            status, payload = replies.pop(0) if replies else (200, {
                "choices": [{"message": {"content": "hello"}}],
                "usage": {"prompt_tokens": 3, "completion_tokens": 2}})
            self.send_response(status)
            self.end_headers()
            self.wfile.write(json.dumps(payload).encode())

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}/v1", seen, replies
    server.shutdown()


def test_a_model_call_reaches_the_provider_with_its_key(provider):
    url, seen, _ = provider
    budget = FakeBudget()
    fn = make_model_fn(OpenAICompatibleClient(url, KEY), "my/model", budget,
                       max_output_tokens=50)
    assert fn("hi") == "hello"
    assert seen[0]["path"] == "/v1/chat/completions"
    assert seen[0]["auth"] == f"Bearer {KEY}"
    assert seen[0]["body"]["model"] == "my/model"
    assert seen[0]["body"]["max_tokens"] == 50
    assert budget.calls[0]["input_tokens"] == 3


def test_the_token_limit_can_use_another_field_name(provider):
    url, seen, _ = provider
    OpenAICompatibleClient(url, KEY, max_tokens_param="max_completion_tokens").inference_chat(
        "m", [], max_output_tokens=50)
    assert seen[0]["body"]["max_completion_tokens"] == 50
    assert "max_tokens" not in seen[0]["body"]


@pytest.mark.parametrize("status,retried", [(429, True), (503, True), (401, False), (400, False)])
def test_only_temporary_failures_are_retried(provider, status, retried):
    url, _, replies = provider
    replies.append((status, {"error": "no"}))
    with pytest.raises(ModelCallError) as e:
        OpenAICompatibleClient(url, KEY).inference_chat("m", [])
    assert _is_transient(e.value) is retried
    assert KEY not in str(e.value)


def test_a_dead_provider_is_retried():
    with pytest.raises(ModelCallError) as e:
        OpenAICompatibleClient("http://127.0.0.1:9/v1", KEY).inference_chat("m", [], timeout=2)
    assert _is_transient(e.value)


def test_sail_inference_stays_the_default():
    sail = object()
    assert make_chat_client(ModelsConfig(), sail) is sail


def test_another_provider_uses_its_own_key(monkeypatch):
    models = ModelsConfig(base_url="https://models.example.com/v1", api_key_env="MY_KEY")
    monkeypatch.delenv("MY_KEY", raising=False)
    with pytest.raises(ValueError, match="MY_KEY"):
        make_chat_client(models, object())
    monkeypatch.setenv("MY_KEY", KEY)
    client = make_chat_client(models, object())
    assert client.url == "https://models.example.com/v1/chat/completions"


def test_the_box_policy_only_adds_the_key_to_model_calls():
    models = ModelsConfig(base_url="https://models.example.com/api/v1/", box_secret="S")
    assert policy_document(models) == {"rules": {"models.example.com": [{
        "match": {"method": "POST", "path": {"equals": "/api/v1/chat/completions"}},
        "request": {"set": {"headers": {"authorization": "Bearer ${secrets.S}"}}},
    }]}}


@pytest.mark.parametrize("url", ["http://models.example.com/v1", "https://localhost:8000/v1"])
def test_boxes_need_a_public_https_provider(url):
    with pytest.raises(ValueError):
        policy_document(ModelsConfig(base_url=url))


def test_your_prices_come_first():
    prices = {"GLM-5.3": (10.0, 20.0)}
    assert BudgetManager.token_cost_usd("GLM-5.3", 10**6, 10**6, prices) == 30.0
    assert BudgetManager.token_cost_usd("GLM-5.3", 10**6, 10**6) == pytest.approx(2.8)


def _unknown_keys(values: dict, model: type[BaseModel], where: str = "") -> list[str]:
    unknown = []
    for key, value in values.items():
        field = model.model_fields.get(key)
        if field is None:
            unknown.append(where + key)
        elif isinstance(value, dict) and isinstance(field.annotation, type) \
                and issubclass(field.annotation, BaseModel):
            unknown += _unknown_keys(value, field.annotation, f"{where}{key}.")
    return unknown


def test_every_setting_in_the_example_config_exists():
    values = tomllib.loads((REPO / ".hindsight.example.toml").read_text())
    assert _unknown_keys(values, HindsightConfig) == []
    HindsightConfig(**values)


def test_workers_are_pointed_at_your_provider():
    from hindsight.executor import Executor

    class Store:
        def kv_set(self, *a): pass
        def last_user_message_id(self, *a): return 0
        def user_text_for_session(self, *a): return ["hi"]

    ex = Executor.__new__(Executor)
    ex.store = Store()
    ex.cfg = HindsightConfig(models=ModelsConfig(
        provider="acme", base_url="https://models.example.com/v1", worker="big-model"))
    plan = type("Plan", (), {"strategy": "open_redesign"})()
    task = ex._task_json("ex", plan, "opencode", "base", "s")
    assert task["model"] == "acme/big-model"
    assert task["base_url"] == "https://models.example.com/v1"
    assert task["api_key"] == ex.cfg.models.box_key_placeholder
