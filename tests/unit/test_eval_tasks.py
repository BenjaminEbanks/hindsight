"""Staged-task format tests.

The hidden tests of every shipped task are themselves VERIFIED here: they
must pass against a correct final implementation and fail against the
naive stage-1 design they are meant to catch (an eval whose instruments
don't discriminate would quietly fake results).
"""

from __future__ import annotations

import json

import pytest

from hindsight.eval.scoring import run_hidden_tests
from hindsight.eval.tasks import Requirement, Stage, Task

TASKS_DIR = "eval/tasks"

# ------------------------------------------------------- reference solutions

COUNTER_FINAL = (
    "import os\n"
    "\n"
    "\n"
    "class Counter:\n"
    "    def __init__(self):\n"
    "        self._path = os.environ.get(\"COUNTER_STORE\", \"counter_state\")\n"
    "\n"
    "    def _read(self):\n"
    "        try:\n"
    "            with open(self._path) as f:\n"
    "                return int(f.read() or 0)\n"
    "        except FileNotFoundError:\n"
    "            return 0\n"
    "\n"
    "    def increment(self):\n"
    "        v = self._read() + 1\n"
    "        with open(self._path, \"w\") as f:\n"
    "            f.write(str(v))\n"
    "        return v\n"
    "\n"
    "    def get_value(self):\n"
    "        return self._read()\n"
)

COUNTER_NAIVE = (
    "class Counter:\n"
    "    def __init__(self):\n"
    "        self._n = 0\n"
    "\n"
    "    def increment(self):\n"
    "        self._n += 1\n"
    "        return self._n\n"
    "\n"
    "    def get_value(self):\n"
    "        return self._n\n"
)

CONFIG_FINAL = (
    "import os\n"
    "\n"
    "\n"
    "class ConfigurationError(Exception):\n"
    "    pass\n"
    "\n"
    "\n"
    "_FIELDS = {\"host\": (\"0.0.0.0\", str), \"port\": (8080, int),\n"
    "           \"timeout\": (30, int)}\n"
    "\n"
    "\n"
    "class Settings:\n"
    "    def __init__(self, host, port, timeout):\n"
    "        self.host = host\n"
    "        self.port = port\n"
    "        self.timeout = timeout\n"
    "\n"
    "\n"
    "def load_settings(env=None):\n"
    "    env = os.environ if env is None else env\n"
    "    values = {}\n"
    "    for name, (default, typ) in _FIELDS.items():\n"
    "        raw = env.get(\"APP_\" + name.upper())\n"
    "        if raw is None:\n"
    "            values[name] = default\n"
    "            continue\n"
    "        try:\n"
    "            v = typ(raw)\n"
    "        except ValueError:\n"
    "            raise ConfigurationError(f\"invalid value for {name}: {raw!r}\")\n"
    "        if name == \"port\" and not (0 < v < 65536):\n"
    "            raise ConfigurationError(f\"port out of range: {v}\")\n"
    "        if name == \"timeout\" and v < 0:\n"
    "            raise ConfigurationError(f\"timeout must be non-negative: {v}\")\n"
    "        values[name] = v\n"
    "    return Settings(**values)\n"
)

CONFIG_NAIVE = (
    "class Settings:\n"
    "    def __init__(self):\n"
    "        self.host = \"0.0.0.0\"\n"
    "        self.port = 8080\n"
    "        self.timeout = 30\n"
    "\n"
    "\n"
    "def load_settings():\n"
    "    return Settings()\n"
)

TODOS_FINAL = (
    "class TodoList:\n"
    "    def __init__(self):\n"
    "        self._items = []\n"
    "\n"
    "    def add(self, title, due=None):\n"
    "        self._items.append([title, False, due])\n"
    "\n"
    "    def list(self):\n"
    "        return [tuple(x) for x in self._items]\n"
    "\n"
    "    def mark_done(self, title):\n"
    "        for x in self._items:\n"
    "            if x[0] == title:\n"
    "                x[1] = True\n"
    "\n"
    "    def pending(self):\n"
    "        return [tuple(x) for x in self._items if not x[1]]\n"
)

REFERENCE_FINAL = {
    "counters": {"counter.py": COUNTER_FINAL},
    "settings": {"config.py": CONFIG_FINAL},
    "todos": {"todos.py": TODOS_FINAL},
}
NAIVE_STAGE1 = {
    "counters": {"counter.py": COUNTER_NAIVE},
    "settings": {"config.py": CONFIG_NAIVE},
    "todos": None,  # no-change task: the naive design is never broken
}


# ------------------------------------------------------------------ tests

def test_all_tasks_load():
    tasks = Task.load_dir(TASKS_DIR)
    assert [t.id for t in tasks] == ["counters", "settings", "todos"]
    assert [t.no_change for t in tasks] == [False, False, True]


def test_ground_truth_accumulates():
    task = Task.load(f"{TASKS_DIR}/counters.json")
    gt = {r.id: r.text for r in task.ground_truth()}
    assert set(gt) == {"R1", "R2", "R3"}
    assert "shared across processes" in gt["R2"]


def test_hidden_tests_accumulate():
    task = Task.load(f"{TASKS_DIR}/todos.json")
    assert sorted(task.hidden_tests()) == [
        "test_stage1_basic.py", "test_stage2_due.py", "test_stage3_pending.py"]


def test_materialize_writes_repo(tmp_path):
    task = Task.load(f"{TASKS_DIR}/counters.json")
    repo = task.materialize(tmp_path / "repo")
    assert (repo / "README.md").exists()
    with pytest.raises(FileExistsError):
        task.materialize(tmp_path / "repo")


def test_loader_rejects_id_mismatch(tmp_path):
    bad = {"id": "wrong", "title": "t", "no_change": False,
           "initial_repo": {}, "stages": [
               {"message": "hi", "requirements": [], "hidden_tests": {}}]}
    p = tmp_path / "someid.json"
    p.write_text(json.dumps(bad))
    with pytest.raises(ValueError, match="does not match"):
        Task.load(p)


def test_empty_stages_rejected():
    with pytest.raises(ValueError):
        Task(id="x", title="t", stages=[])


@pytest.mark.parametrize("task_id", ["counters", "settings", "todos"])
def test_hidden_tests_pass_on_correct_design(tmp_path, task_id):
    task = Task.load(f"{TASKS_DIR}/{task_id}.json")
    repo = task.materialize(tmp_path / task_id)
    for rel, content in REFERENCE_FINAL[task_id].items():
        (repo / rel).write_text(content)
    result = run_hidden_tests(task, repo, timeout=300)
    assert result.ran, result.error
    assert result.failed == 0, result.output
    assert result.passed > 0


@pytest.mark.parametrize("task_id", ["counters", "settings"])
def test_hidden_tests_catch_the_naive_design(tmp_path, task_id):
    """The instrument must discriminate: the stage-1 design (which the
    later requirements break) fails the hidden tests."""
    task = Task.load(f"{TASKS_DIR}/{task_id}.json")
    repo = task.materialize(tmp_path / task_id)
    for rel, content in NAIVE_STAGE1[task_id].items():
        (repo / rel).write_text(content)
    result = run_hidden_tests(task, repo, timeout=300)
    assert result.ran, result.error
    assert result.failed > 0, (
        f"{task_id}: hidden tests pass on the naive design — they cannot "
        "detect the design break they exist for")


def test_hidden_tests_leave_no_trace(tmp_path):
    task = Task.load(f"{TASKS_DIR}/todos.json")
    repo = task.materialize(tmp_path / "repo")
    (repo / "todos.py").write_text(TODOS_FINAL)
    run_hidden_tests(task, repo, timeout=300)
    assert not (repo / ".hindsight_eval_hidden").exists()
    # No stray hidden-test files; __pycache__ from imports is unavoidable.
    leftovers = [p.name for p in repo.iterdir()
                 if p.name.startswith("test_") or "hidden" in p.name]
    assert leftovers == []


def test_hidden_test_env_is_scrubbed(monkeypatch):
    """Inherited venv state must not leak into scoring: an outer venv's
    editable install puts its project root on sys.path, so a module
    missing from the staged repo can import from OUTSIDE it and a broken
    design scores as passing (found live)."""
    from hindsight.eval.scoring import _clean_env
    monkeypatch.setenv("VIRTUAL_ENV", "/somewhere/.venv")
    monkeypatch.setenv("UV_PROJECT_ENVIRONMENT", "/somewhere/.venv")
    monkeypatch.setenv("PYTHONPATH", "/somewhere/outer")
    monkeypatch.setenv("KEEP_ME", "yes")
    env = _clean_env()
    assert "VIRTUAL_ENV" not in env
    assert "UV_PROJECT_ENVIRONMENT" not in env
    assert "PYTHONPATH" not in env
    assert env["KEEP_ME"] == "yes"
