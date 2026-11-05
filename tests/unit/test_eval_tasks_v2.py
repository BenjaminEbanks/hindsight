"""v2 staged-task discrimination tests.

Same bar as tests/unit/test_eval_tasks.py: every shipped v2 task's
hidden tests must pass against a correct final implementation and fail
against the naive stage-1 design they are meant to catch. The v2 set
additionally stages simulated-user phenomena: registers vary per task, breaks ride behind praise
or venting, anti-supersede sentences protect stable requirements, and
the metrics arc stages a non_goal reversal (I know I said no class).
"""

from __future__ import annotations

import pytest

from hindsight.eval.scoring import run_hidden_tests
from hindsight.eval.tasks import Task

TASKS_DIR = "eval/tasks/v2"


BOOKMARKS_NAIVE = (
    "import json\n" +
    "\n" +
    "\n" +
    "class BookmarkStore:\n" +
    "    def __init__(self):\n" +
    "        self._entries = []  # [title, url]\n" +
    "\n" +
    "    def add(self, title, url):\n" +
    "        self._entries.append((title, url))\n" +
    "\n" +
    "    def entries(self):\n" +
    "        return list(self._entries)\n" +
    "\n" +
    "    def save(self, path):\n" +
    "        with open(path, \"w\") as f:\n" +
    "            json.dump([{\"title\": t, \"url\": u} for t, u in self._entries], f)\n" +
    "\n" +
    "    @classmethod\n" +
    "    def load(cls, path):\n" +
    "        store = cls()\n" +
    "        with open(path) as f:\n" +
    "            data = json.load(f)\n" +
    "        for item in data:\n" +
    "            store.add(item[\"title\"], item[\"url\"])\n" +
    "        return store\n")

BOOKMARKS_FINAL = (
    "import json\n" +
    "\n" +
    "\n" +
    "class BookmarkStore:\n" +
    "    def __init__(self):\n" +
    "        self._folders = {}  # folder -> [[title, url], ...]\n" +
    "\n" +
    "    def add(self, title, url, folder=\"general\"):\n" +
    "        self._folders.setdefault(folder, []).append([title, url])\n" +
    "\n" +
    "    def entries(self, folder=None):\n" +
    "        if folder is not None:\n" +
    "            return [tuple(e) for e in self._folders.get(folder, [])]\n" +
    "        out = []\n" +
    "        for name in sorted(self._folders):\n" +
    "            out.extend(tuple(e) for e in self._folders[name])\n" +
    "        return out\n" +
    "\n" +
    "    def folders(self):\n" +
    "        return sorted(self._folders)\n" +
    "\n" +
    "    def save(self, path):\n" +
    "        data = {\"version\": 2, \"folders\": self._folders}\n" +
    "        with open(path, \"w\") as f:\n" +
    "            json.dump(data, f)\n" +
    "\n" +
    "    @classmethod\n" +
    "    def load(cls, path):\n" +
    "        with open(path) as f:\n" +
    "            data = json.load(f)\n" +
    "        store = cls()\n" +
    "        if isinstance(data, list):\n" +
    "            # v1 (beta) files: a flat list of {\"title\", \"url\"}\n" +
    "            for item in data:\n" +
    "                store.add(item[\"title\"], item[\"url\"])\n" +
    "        else:\n" +
    "            for folder, items in data[\"folders\"].items():\n" +
    "                for title, url in items:\n" +
    "                    store.add(title, url, folder=folder)\n" +
    "        return store\n")

CACHE_NAIVE = (
    "class Cache:\n" +
    "    def __init__(self):\n" +
    "        self._data = {}\n" +
    "\n" +
    "    def get_or_load(self, key, loader):\n" +
    "        if key not in self._data:\n" +
    "            self._data[key] = loader()\n" +
    "        return self._data[key]\n" +
    "\n" +
    "    def get(self, key):\n" +
    "        return self._data.get(key)\n" +
    "\n" +
    "    def size(self):\n" +
    "        return len(self._data)\n")

CACHE_FINAL = (
    "import threading\n" +
    "from collections import OrderedDict, defaultdict\n" +
    "\n" +
    "\n" +
    "class Cache:\n" +
    "    def __init__(self, maxsize=None):\n" +
    "        self.maxsize = maxsize\n" +
    "        self._lock = threading.Lock()\n" +
    "        self._data = OrderedDict()\n" +
    "        self._key_locks = defaultdict(threading.Lock)\n" +
    "\n" +
    "    def get_or_load(self, key, loader):\n" +
    "        with self._lock:\n" +
    "            if key in self._data:\n" +
    "                self._data.move_to_end(key)\n" +
    "                return self._data[key]\n" +
    "            klock = self._key_locks[key]\n" +
    "        with klock:  # single-flight: one loader per key\n" +
    "            with self._lock:  # double-check: it may be loaded by now\n" +
    "                if key in self._data:\n" +
    "                    self._data.move_to_end(key)\n" +
    "                    return self._data[key]\n" +
    "            value = loader()\n" +
    "            with self._lock:\n" +
    "                self._data[key] = value\n" +
    "                if self.maxsize is not None and len(self._data) > self.maxsize:\n" +
    "                    self._data.popitem(last=False)\n" +
    "            return value\n" +
    "\n" +
    "    def get(self, key):\n" +
    "        with self._lock:\n" +
    "            if key in self._data:\n" +
    "                self._data.move_to_end(key)\n" +
    "                return self._data[key]\n" +
    "            return None\n" +
    "\n" +
    "    def size(self):\n" +
    "        with self._lock:\n" +
    "            return len(self._data)\n")

CART_NAIVE = (
    "class Cart:\n" +
    "    def __init__(self):\n" +
    "        self._lines = []  # [sku, unit price, qty]\n" +
    "\n" +
    "    def add(self, sku, price, qty=1):\n" +
    "        self._lines.append([sku, price, qty])\n" +
    "\n" +
    "    def items(self):\n" +
    "        return [tuple(l) for l in self._lines]\n" +
    "\n" +
    "    def total(self):\n" +
    "        return sum(price * qty for _, price, qty in self._lines)\n")

CART_FINAL = (
    "class Cart:\n" +
    "    def __init__(self):\n" +
    "        self._lines = []  # [sku, unit price, qty]\n" +
    "\n" +
    "    def add(self, sku, price, qty=1):\n" +
    "        self._lines.append([sku, price, qty])\n" +
    "\n" +
    "    def items(self):\n" +
    "        return [tuple(l) for l in self._lines]\n" +
    "\n" +
    "    def total(self):\n" +
    "        return sum(price * qty for _, price, qty in self._lines)\n" +
    "\n" +
    "    def remove(self, sku):\n" +
    "        self._lines = [l for l in self._lines if l[0] != sku]\n" +
    "\n" +
    "    def apply_discount(self, percent):\n" +
    "        for l in self._lines:\n" +
    "            l[1] = round(l[1] * (100 - percent) / 100, 2)\n" +
    "\n" +
    "    def most_expensive(self):\n" +
    "        return max(self._lines, key=lambda l: l[1] * l[2])[0]\n")

EVENTS_NAIVE = (
    "def read_events(path):\n" +
    "    events = []\n" +
    "    with open(path) as f:\n" +
    "        for line in f:\n" +
    "            line = line.strip()\n" +
    "            if not line:\n" +
    "                continue\n" +
    "            lvl, message = line.split(\"|\", 1)\n" +
    "            events.append({\"level\": lvl, \"message\": message})\n" +
    "    return events\n")

EVENTS_FINAL = (
    "def iterate(source, level=None):\n" +
    "    if hasattr(source, \"readline\"):\n" +
    "        f = source\n" +
    "        opened = False\n" +
    "    else:\n" +
    "        f = open(source)\n" +
    "        opened = True\n" +
    "    try:\n" +
    "        for line in f:\n" +
    "            line = line.strip()\n" +
    "            if not line:\n" +
    "                continue\n" +
    "            lvl, message = line.split(\"|\", 1)\n" +
    "            if level is None or lvl == level:\n" +
    "                yield {\"level\": lvl, \"message\": message}\n" +
    "    finally:\n" +
    "        if opened:\n" +
    "            f.close()\n" +
    "\n" +
    "\n" +
    "def read_events(source):\n" +
    "    return list(iterate(source))\n")

EXPENSES_NAIVE = (
    "class ExpenseLog:\n" +
    "    def __init__(self):\n" +
    "        self._totals = {}\n" +
    "\n" +
    "    def record(self, category, amount):\n" +
    "        self._totals[category] = self._totals.get(category, 0) + amount\n" +
    "\n" +
    "    def total(self, category):\n" +
    "        return self._totals.get(category, 0)\n" +
    "\n" +
    "    def categories(self):\n" +
    "        return sorted(self._totals)\n")

EXPENSES_FINAL = (
    "class ExpenseLog:\n" +
    "    def __init__(self):\n" +
    "        self._entries = {}  # category -> [amounts]\n" +
    "\n" +
    "    def record(self, category, amount):\n" +
    "        self._entries.setdefault(category, []).append(amount)\n" +
    "\n" +
    "    def total(self, category):\n" +
    "        return sum(self._entries.get(category, ()))\n" +
    "\n" +
    "    def categories(self):\n" +
    "        return sorted(k for k, v in self._entries.items() if v)\n" +
    "\n" +
    "    def entries(self, category):\n" +
    "        return list(self._entries.get(category, ()))\n" +
    "\n" +
    "    def undo(self, category):\n" +
    "        if self._entries.get(category):\n" +
    "            self._entries[category].pop()\n" +
    "\n" +
    "    def remove(self, category, index):\n" +
    "        del self._entries[category][index]\n")

IMPORTER_NAIVE = (
    "class Importer:\n" +
    "    def __init__(self, rows, on_item):\n" +
    "        self._rows = list(rows)\n" +
    "        self._on_item = on_item\n" +
    "\n" +
    "    def run(self):\n" +
    "        processed = 0\n" +
    "        for row in self._rows:\n" +
    "            self._on_item(row)\n" +
    "            processed += 1\n" +
    "        return processed\n")

IMPORTER_FINAL = (
    "import threading\n" +
    "\n" +
    "\n" +
    "class Importer:\n" +
    "    def __init__(self, rows, on_item):\n" +
    "        self._rows = list(rows)\n" +
    "        self._on_item = on_item\n" +
    "        self._cancel = threading.Event()\n" +
    "        self._state = \"never run\"\n" +
    "\n" +
    "    def cancel(self):\n" +
    "        self._cancel.set()\n" +
    "\n" +
    "    def run(self):\n" +
    "        self._cancel.clear()\n" +
    "        self._state = \"running\"\n" +
    "        processed = 0\n" +
    "        for row in self._rows:\n" +
    "            if self._cancel.is_set():\n" +
    "                self._state = \"cancelled\"\n" +
    "                return processed\n" +
    "            self._on_item(row)\n" +
    "            processed += 1\n" +
    "        self._state = \"completed\"\n" +
    "        return processed\n" +
    "\n" +
    "    def status(self):\n" +
    "        return self._state\n")

MEETINGS_NAIVE = (
    "class Scheduler:\n" +
    "    def __init__(self):\n" +
    "        self._events = {}\n" +
    "\n" +
    "    def add(self, title, when):\n" +
    "        self._events[title] = when\n" +
    "\n" +
    "    def list(self):\n" +
    "        return sorted((title, when) for title, when in self._events.items())\n")

MEETINGS_FINAL = (
    "from datetime import datetime, timedelta\n" +
    "from zoneinfo import ZoneInfo\n" +
    "\n" +
    "_FMT = \"%Y-%m-%d %H:%M\"\n" +
    "DEFAULT_TZ = \"America/New_York\"\n" +
    "\n" +
    "\n" +
    "class Scheduler:\n" +
    "    def __init__(self):\n" +
    "        self._events = {}\n" +
    "\n" +
    "    def add(self, title, when, tz=None, repeat=None):\n" +
    "        self._events[title] = {\n" +
    "            \"when\": datetime.strptime(when, _FMT),\n" +
    "            \"tz\": tz or DEFAULT_TZ,\n" +
    "            \"repeat\": repeat,\n" +
    "        }\n" +
    "\n" +
    "    def _display(self, wall, from_tz, out_tz):\n" +
    "        aware = wall.replace(tzinfo=ZoneInfo(from_tz))\n" +
    "        if out_tz != from_tz:\n" +
    "            aware = aware.astimezone(ZoneInfo(out_tz))\n" +
    "        return aware.strftime(_FMT)\n" +
    "\n" +
    "    def list(self, tz=None):\n" +
    "        out = []\n" +
    "        for title, ev in self._events.items():\n" +
    "            local = ev[\"when\"].replace(tzinfo=ZoneInfo(ev[\"tz\"]))\n" +
    "            if tz is not None:\n" +
    "                local = local.astimezone(ZoneInfo(tz))\n" +
    "            out.append((title, local.strftime(_FMT)))\n" +
    "        out.sort(key=lambda x: x[1])\n" +
    "        return out\n" +
    "\n" +
    "    def next_dates(self, title, n, tz=None):\n" +
    "        ev = self._events[title]\n" +
    "        out_tz = tz or ev[\"tz\"]\n" +
    "        out = []\n" +
    "        day = ev[\"when\"]\n" +
    "        for _ in range(n):\n" +
    "            out.append(self._display(day, ev[\"tz\"], out_tz))\n" +
    "            if ev[\"repeat\"] != \"daily\":\n" +
    "                break\n" +
    "            day = day + timedelta(days=1)\n" +
    "        return out\n")

METRICS_NAIVE = (
    "_counters = {}\n" +
    "\n" +
    "\n" +
    "def inc(name, amount=1):\n" +
    "    _counters[name] = _counters.get(name, 0) + amount\n" +
    "\n" +
    "\n" +
    "def get(name):\n" +
    "    return _counters.get(name, 0)\n")

METRICS_FINAL = (
    "class Metrics:\n" +
    "    def __init__(self):\n" +
    "        self._series = {}  # name -> {frozenset(labels): value}\n" +
    "\n" +
    "    def inc(self, name, amount=1, labels=None):\n" +
    "        per = self._series.setdefault(name, {})\n" +
    "        key = frozenset(labels.items()) if labels else frozenset()\n" +
    "        per[key] = per.get(key, 0) + amount\n" +
    "\n" +
    "    def get(self, name, labels=None):\n" +
    "        per = self._series.get(name, {})\n" +
    "        if labels is not None:\n" +
    "            return per.get(frozenset(labels.items()), 0)\n" +
    "        return sum(per.values())\n" +
    "\n" +
    "\n" +
    "_default = Metrics()\n" +
    "\n" +
    "\n" +
    "def inc(name, amount=1, labels=None):\n" +
    "    _default.inc(name, amount=amount, labels=labels)\n" +
    "\n" +
    "\n" +
    "def get(name, labels=None):\n" +
    "    return _default.get(name, labels=labels)\n")

NOTIFY_NAIVE = (
    "class Notifier:\n" +
    "    def __init__(self, sender):\n" +
    "        self._sender = sender\n" +
    "\n" +
    "    def notify(self, user, message):\n" +
    "        self._sender(user, message)\n" +
    "        return True\n")

NOTIFY_FINAL = (
    "class Notifier:\n" +
    "    def __init__(self, sender, retries=2):\n" +
    "        self._sender = sender\n" +
    "        self._retries = retries\n" +
    "        self._done = set()\n" +
    "\n" +
    "    def notify(self, user, message, delivery_id=None):\n" +
    "        if delivery_id is not None and delivery_id in self._done:\n" +
    "            return False\n" +
    "        attempts = self._retries + 1\n" +
    "        for attempt in range(attempts):\n" +
    "            try:\n" +
    "                self._sender(user, message)\n" +
    "            except Exception:\n" +
    "                if attempt == attempts - 1:\n" +
    "                    raise\n" +
    "            else:\n" +
    "                if delivery_id is not None:\n" +
    "                    self._done.add(delivery_id)\n" +
    "                return True\n")

REFERENCE_FINAL = {
    "bookmarks": {"bookmarks.py": BOOKMARKS_FINAL},
    "cache": {"cache.py": CACHE_FINAL},
    "cart": {"cart.py": CART_FINAL},
    "events": {"events.py": EVENTS_FINAL},
    "expenses": {"expenses.py": EXPENSES_FINAL},
    "importer": {"importer.py": IMPORTER_FINAL},
    "meetings": {"scheduler.py": MEETINGS_FINAL},
    "metrics": {"metrics.py": METRICS_FINAL},
    "notify": {"notify.py": NOTIFY_FINAL},
}

NAIVE_STAGE1 = {
    "bookmarks": {"bookmarks.py": BOOKMARKS_NAIVE},
    "cache": {"cache.py": CACHE_NAIVE},
    "cart": None,  # no-change task: the naive design is never broken
    "events": {"events.py": EVENTS_NAIVE},
    "expenses": {"expenses.py": EXPENSES_NAIVE},
    "importer": {"importer.py": IMPORTER_NAIVE},
    "meetings": {"scheduler.py": MEETINGS_NAIVE},
    "metrics": {"metrics.py": METRICS_NAIVE},
    "notify": {"notify.py": NOTIFY_NAIVE},
}


# ------------------------------------------------------------------ tests

def test_all_v2_tasks_load():
    tasks = Task.load_dir(TASKS_DIR)
    assert [t.id for t in tasks] == ["bookmarks", "cache", "cart", "events", "expenses", "importer", "meetings", "metrics", "notify"]
    assert [t.no_change for t in tasks] == [t.id in NO_CHANGE_IDS for t in tasks]


NO_CHANGE_IDS = [t for t in REFERENCE_FINAL if t not in NAIVE_STAGE1 or NAIVE_STAGE1[t] is None]


@pytest.mark.parametrize("task_id", sorted(REFERENCE_FINAL))
def test_hidden_tests_pass_on_correct_design(tmp_path, task_id):
    task = Task.load(f"{TASKS_DIR}/{task_id}.json")
    repo = task.materialize(tmp_path / task_id)
    for rel, content in REFERENCE_FINAL[task_id].items():
        (repo / rel).write_text(content)
    result = run_hidden_tests(task, repo, timeout=300)
    assert result.ran, result.error
    assert result.failed == 0, result.output
    assert result.passed > 0


@pytest.mark.parametrize("task_id", [t for t in sorted(NAIVE_STAGE1) if NAIVE_STAGE1[t] is not None])
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

