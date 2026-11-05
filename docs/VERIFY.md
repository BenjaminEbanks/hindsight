# External APIs: what we checked

A dated log, kept as work went on. Every external API was checked against the installed version
before use, and the result was recorded here. Item IDs like V13 are tracking labels from early
in the project, and older entries may describe designs that have since changed.

Environment at time of writing: macOS arm64, Python 3.11.7, uv,
sail-sdk 0.17.2 (installed and inspected). OpenCode / Pi / Codex CLIs are
NOT installed locally; they are used only inside boxes.

## Verified by SDK inspection (sail-sdk 0.17.2)

| Item | Result |
|---|---|
| `Sailbox.create(app=..., name=..., image=..., max_lifetime_seconds=..., visibility=...)` | Present. App via `App.find(name, mint_if_missing=True)`. |
| `box.exec(cmd, background=True, ...)`, `box.run(cmd)` | Present; `ExecResult` has `.exit_code/.stdout/.stderr/.timed_out`. |
| `box.fs.write_files / write / read / upload_file / upload_dir / download_file / exists / mkdir` | Present. |
| `box.checkpoint(ttl_seconds=..., name=...)` | Present → **V13 RESOLVED**: TTL supported. |
| `Sailbox.from_checkpoint(checkpoint_id, name=...)` | Present. **No `Sailbox.fork` exists** — "forking" = checkpoint a running box, then `from_checkpoint` (background processes continue, `exec`-started commands stop). Sufficient for the causal-fork eval (Section 11.6), with the documented reconnect caveat. |
| `box.terminate()` | Present, idempotent per docstring. |
| `Sailbox.list(search=..., name=..., status=...)` | Present — enough for the reaper. |
| `sail.inference.chat.completions.create(model, messages, headers=..., **payload)` | Present; custom headers supported → **V9 partially RESOLVED** (headers work; whether `X-SailboxId` sleeps the box is a runtime question). Responses API also present in `sail.inference.responses` → V16 likely OK, runtime check pending. |
| Auth | `SAIL_API_KEY` env var or `sail auth login` (from `sail/_config.py`). |
| Images | **corrected M-B**: `sail.Image.debian("arm64")` (the `Image` namespace; `sail.image.debian` does NOT exist — `sail.image` is the module) + `.apt_install("git", ...)` + `.pip_install("pytest")` (varargs, not a list); also `from_dockerfile`, `from_registry`. Verified by `AttributeError` on the old form + live introspection. |

## Verified by running (local, no Sail credentials)

| Item | Result |
|---|---|
| Snapshotter never touches user's `.git` (100-snapshot test) | PASS (`tests/unit/test_snapshot.py`) |
| Store state machine rejects illegal transitions | PASS |
| Ledger grounding / locks / transitions / assumption evidence | PASS |
| Trigger score + firing rule; budget refusals; gate stage 0 | PASS |
| Orchestrator end-to-end with FakeSailClient (labeled unit mock) | PASS |
| Live smoke: `hindsight up` + plugin-style event → shadow snapshot at `refs/hindsight/s1/1/pre` | PASS |

## Verified by running (live, with SAIL_API_KEY, 2026-10-29/30)

| Item | Result |
|---|---|
| `create_box` / `run` / `upload_file` / `download_file` / `write_files` / `read_file` / `terminate` | PASS (`tests/contract/test_sail_runtime.py::test_box_lifecycle_roundtrip`) |
| **Fork path**: `checkpoint(ttl_seconds=3600)` then `from_checkpoint` → new box preserves disk state | PASS (`test_checkpoint_and_fork`). Causal-fork eval design (Section 11.6) is viable; reconnect caveat documented. |
| 50 concurrent boxes created and terminated | PASS (`test_concurrency`) |
| Repeated create latency (10 runs, p50/p95) | PASS (`test_latency_stability`); per-run numbers printed by the test |
| Inference chat with tool calling | PASS for `zai-org/GLM-5.3` and `moonshotai/Kimi-K3` |
| Inference usage fields | `prompt_tokens` / `completion_tokens` (OpenAI-style, NOT `input_tokens`/`output_tokens`) — `models.py` reads both, prefers the real names |
| Model listing endpoint | `GET /v1/models` via `sail._core_client.ApiTransport` |

### M0 model matrix (served by Sail inference, 2026-10-30)

```
moonshotai/Kimi-K3            zai-org/GLM-5.3            zai-org/GLM-5.3-Flash
deepseek-ai/DeepSeek-V4-Flash-0731    deepseek-ai/DeepSeek-V4.1-Flash-1017
deepseek-ai/DeepSeek-V4-Pro-0813     google/gemma-4-31B-it  google/gemma-4-12B-it
nvidia/Gemma-4-31B-IT-NVFP4   Qwen/Qwen3.6-35B-A3B
```

- No OpenAI/Anthropic models are served on Sail inference. The design's
  `gpt-5*`/`claude-*` placeholders do not exist there; OpenAI credits would
  apply only to direct OpenAI API use (out of the Sail path).
- `Qwen/Qwen3.6-35B-A3B` rejects synchronous requests without streaming —
  excluded from all sync roles (ledger, judge, driver-side calls).
- Role assignment (Section 8), resolved into `ModelsConfig` defaults:
  ledger/nudge/adapter = GLM-5.3-Flash; worker/test_author/simuser = GLM-5.3;
  judge = Kimi-K3 (different family from the worker, per the
  self-preference mitigation).
- Token prices in `budget.py` are placeholders pending reconciliation against
  Sail billing (TBD; stop rule unchanged).

## Verified by running (eval main-agent CLI probe, 2026-10-31) — V-E1

Bundled CLI `/Applications/OpenCode.app/Contents/Resources/opencode-cli`:

| Item | Result |
|---|---|
| `run --session <id> --auto --format json [--model m] <msg>` | Works. Emits a **JSONL event stream** (one JSON object per line), not one JSON doc. |
| Reply text | Events `{"type":"text","part":{"type":"text","text":...}}`; the harness joins them (`_extract_reply`). |
| Errors | `{"type":"error", "error":{"message":...}}` — and **exit code is still 0**, so the harness scans for error events explicitly (`_scan_events`). |
| Tokens | `step-finish` part carries `tokens:{input,output,reasoning,cache}`; the harness accumulates them (`OpenCodeAgent.token_totals`). |
| Session IDs | Must start with `ses` (`Expected a string starting with "ses"` otherwise). Agent ids are `ses-hs-eval-*`. |
| Default model | Free-tier model; hit `provider.free-limit` (429). The **sail-research provider is authed** for the CLI and works: verified one `sail-research/zai-org/GLM-5.3` call ("Reply with exactly: OK" → "OK"). The live eval main agent defaults to it (billed to Sail, outside the costs table — recorded honestly in the results). |
| **Project-dir binding** (2026-11-01) | The CLI resolves its project directory from the **inherited `$PWD` env var**, not the process cwd: `subprocess.run(cwd=staged)` alone leaves `$PWD` at the parent's directory and the agent edits THAT (diagnosed from the opencode server log: `location services booted directory=<outer>` while the serve process had `cwd=<staged>`). Setting `PWD=<repo>` (or unsetting it) binds correctly — probe-verified both ways, including nested under `uv run`. Necessary AND sufficient together with a git-init'd staged repo + `--standalone` (each alone is not: without `--standalone` the background service's cwd wins; without git-init the CLI walks up to the enclosing repo). |
| **`--fork` returns a continuable session id** (V-E2, 2026-11-01) | PASS. `opencode run --standalone --session ses-hs-e2-base --fork <msg>` returns events under a **new** session id (`ses_e68bfdc...`), which inherits the full transcript: taught a codeword in the base session, the fork replied with it, then continued on its own id and counted all 3 user messages. The **base session stayed untouched** (continued directly afterwards and counted only its own 2). Fork branches in the eval can therefore inherit the conversation transcript instead of starting fresh agents (`hindsight/eval/fork.py` has the design note). |

## Verified by running (OpenCode v2 plugin probe, 2026-11-01) — V-oc-1 / V2-5

Installed OpenCode is **2.2.5**, whose plugin format is v2 — NOT the v1 format
described at `opencode.ai/docs/plugins` (that page documents the old
`export const X = async (ctx) => ({event})` API, which 2.2.5 rejects:
"Plugin must export a default definition with an id and an effect or setup
function"). Correct docs: `opencode.ai/v2/docs/plugins` and
`/v2/docs/build/plugins/server`. All verified live with
`opencode run --standalone` against `hindsight up` (probe repo
`ve2-toast/repo`, plugin at `.opencode/plugins/hindsight/`):

| Item | Result |
|---|---|
| **Plugin format** | `export default {id, setup(ctx)}` (plain object; `Plugin.define` from `@opencode/plugin` not required). `setup` may return a cleanup fn. `plugins` config entries must be **directories**; a `.ts` file path is skipped with a warning. `.opencode/plugins/` auto-discovers direct `.ts`/`.js` files and immediate plugin package directories; a local dir without package.json loads `index`/`server` (.ts/.js) as the server part and `gui-experimental.cjs` at root as the GUI part. |
| **`ctx.event.subscribe({signal})`** | Async iterable over the server's public events. Payload is under **`data`** (not `properties`); the session id is `data.sessionID` (also `durable.aggregateID`). Unknown type names are ignored with a log warning. |
| **v2 event vocabulary** (from the binary, confirmed live) | Turn lifecycle: `session.inbox.enqueued` (item `{type:"user", payload:{text}}` + `inboxID`, fires **before** execution starts) → `session.execution.started` → … → `session.execution.succeeded`/`.failed` → `session.idle` (interactive quiet, did NOT fire in headless runs). Messages: `message.updated`/`message.removed` (did not fire during headless runs). Also `session.compaction.ended`, `session.error`, `file.edited` exist; `message.part.*`/`session.text.delta` stream parts. |
| **`ctx.session.context({sessionID})`** | Returns the full transcript as an array of `{id, type: "user"\|"assistant"\|"idle", text \| content:[{type:"reasoning"\|"tool"\|"text", text}]}`. Role is **`type`**, not `role`; user messages carry plain `text`; assistant text lives in `content` parts of `type:"text"` (reasoning parts excluded by the plugin). |
| **`ctx.session.get({sessionID})`** | Session info incl. `parentID` for child (sub-agent) sessions — the child-session filter works. |
| **`ctx.tool.transform((editor) => editor.add({name, description, input: <JSON Schema>, execute}))`** | Works. `execute(input, context)` gets `context.sessionID`; return `{content: string}`. **Verified live**: the agent called `hindsight_status` and reported the orchestrator's real spend and exploration state. |
| **`ctx.location.directory`** | The plugin instance's project directory (used for the spool file name). |
| **GUI part surfaces** (from the app bundle + `/v2/docs/build/plugins/gui`; desktop app only, **verified live 2026-11-01 — V2-5 done**) | `ctx.toast.show({title, description?, variant: "default"\|"success"\|"error"\|"loading", duration? (ms, default 5000), persistent?, actions?: [{label, run}]})` → `{dismiss()}`; `ctx.attention.notify({title, message?, variant, session?: {server, id}, sound?, id?})` — system notification + sound while no window has focus, click opens the session, `id` dedupes across windows. The **server part has no toast** — headless instances must not consume nudges, so only the GUI part polls `/nudges/pending` and acks. | **Live result**: seeded nudge → in-window toast shown (`toast.show` returned ok) → `attention.notify` returned `{notification:false, sound:true}` (window had focus, so sound only — correct per docs) → ack POST 200 → store row `ack_displayed=1`, pending list drained, next poll found 0. |
| **GUI bundle runtime environment** (from the desktop app's own loader `LSt`, confirmed by failure then success) | Bundles are evaluated via a CommonJS shim — `new Function("require","module","exports", code)` — with **no `process`** and `require("node:*")`/bare Node builtins resolving to **inert proxies**; only browser globals (`fetch`, `setInterval`, `AbortController`, `URL`, …) plus the shared module map (`@opencode/plugin/gui*`, `effect`, `solid-js`, …) exist. A bundle touching `process.env` throws at evaluation and never runs (observed). Exports: `module.exports.default ?? module.exports`, schema-checked (needs ≥1 of `setup`/`main`). |
| **GUI part install location** (from the desktop main-process plugin host + live test) | The window-side plugin host lists plugins from the **sidecar server's global list** (`GET /api/plugin` without a location) — i.e. `~/.config/opencode/plugins/<id>/gui-experimental.cjs` — NOT from a project's `.opencode/plugins`. The project copy is loaded only as the *server* part. So `hindsight init` installs: server part → `<project>/.opencode/plugins/hindsight/`, GUI part → `~/.config/opencode/plugins/hindsight/` (app restart picks it up). |
| **GUI → orchestrator fetch needs CORS** | The window's origin is not `127.0.0.1:8787`, so without `Access-Control-Allow-Origin` every GUI-part fetch fails silently (observed: no toast, no ack, no log). `hindsight up` now enables permissive CORS and exposes `POST /debug/gui` as the GUI part's log sink (the window has no fs) → `~/.hindsight/gui-plugin.log`. |
| **End-to-end (live)** | Plugin installed in a probe repo → `opencode run` → `user_message` (pre-turn snapshot before the agent's edits) + `turn_completed` (4 messages with correct roles, outer run-CLI quotes stripped) → turn `status=ok`, `usable=1 ("settled")`, ledger v1 written via real Sail inference calls, costs recorded. No plugin load errors in the server log. |


## Verified by running (swarm probes, 2026-11-02) — sailbox-per-episode eval

Probes for the parallel-eval redesign (one sailbox per episode, user
direction). Scripts under `$TMPDIR/opencode/probe_box_*.py`.

| Item | Result |
|---|---|
| **Default box stack** (`create_box(image=None)`) | Debian, Linux 6.1.155 **x86_64**, Python 3.11.2 + pip 23.0.1, git/curl/tar/bash present, **no node/npm**, egress to api.sailresearch.com and pypi.org OK, ~600 GB root disk, user is **root**, `/work` does not exist (write under `/root`). |
| **PEP 668** | `pip3 install` into the system interpreter is refused; `--break-system-packages` is required in every box setup script (observed failure, then success). |
| **Default box has NO pytest** (2026-11-02, PAPERCUTS #30) | `python3 -m pytest --version` → "No module named pytest". The swarm SETUP installs `pytest` explicitly and guards on the import check; without it every in-box hidden-test scoring silently recorded failed tests. |
| **Nested sailboxes** | A box with `sail==0.17.2` installed + `SAIL_API_KEY` exported created a child box, ran a command in it, and terminated it — **NESTED-OK**. Treatment/fork arms can run their worker boxes from inside an episode box. |
| **opencode CLI in a box** | The public installer (`opencode.ai/install`) only serves **1.x**; v2.2.5 (the pinned local version) comes from the GitHub release asset: `https://github.com/anomalyco/opencode/releases/download/v2.2.5/opencode-linux-x64.tar.gz` (plain `opencode` binary inside; `--version` → `opencode v2.2.5`). |
| **Box prerequisites for agent runs** | git needs an explicit identity (`git config --global user.email/name`) — the commit-based staging fails otherwise (observed). Provider auth ships as the user's `~/.config/opencode/opencode.json` → `/root/.config/opencode/`. |
| **Full agent turn in-box** | `opencode run --standalone --session <id> --auto --format json --model sail-research/zai-org/GLM-5.3` in a fresh box: 6 events, 0 error events, instructed marker file written with exact content — **PROBE-AGENT OK**. |
| **Concurrent local CLI instances** (earlier, local-pool probe) | Two simultaneous `opencode run --standalone` processes in separate repos: both completed, no cross-contamination. |
| **`Sailbox.exec(background=True)` argument form** (sail-sdk 0.17.2, sailbox.py) | The signature advertises `list[str] \| str`, but the list form RAISES `InvalidArgumentError("background requires a shell command string")`. Failure is recorded per-worker, not raised into the eval: **all 42 explorations of the 2026-11-01 live run failed this way, silently** (PAPERCUTS #28). Fix: `start_background` joins lists with `shlex.join`; verified by the repro in the PAPERCUTS entry and re-probed below. |
| **Multi-hour synchronous `run`** | Dies with its channel when the box disappears (`SailboxHostLostError`; follow-up calls see `SailboxTerminatedError`). Proviso: tonight's observed deaths turned out to be our own reaper (PAPERCUTS #29), not host loss — but the background-exec + 45s-poll design is kept because the failure mode is real for genuine host loss, and the polling path is strictly more observable (on-box `episode.log`, exit-code file). |

## Unverified — needs a live box (remaining M0 items)

| ID | Item | Planned check |
|---|---|---|
| V-pi-1 (V7) | ~~Pi RPC line protocol~~ **ABANDONED 2026-11-02**: the design's worker-side `pi` CLI has no known install channel and was never installed — every swarm worker died at Popen (the dead-workforce bug, PAPERCUTS #35). Workers now run headless opencode v2.2.5 (env.HARNESS_INSTALL, commit 5dc4252). The Pi harness code remains in box/driver.py but is no longer reachable by default. | n/a |
| V-oc-w | ~~Worker-side opencode serve+HTTP harness~~ **REJECTED 2026-11-02 06:33**: v2.2.5 has no POST /session (405; real API under /api/*), serve password auth unresolvable from the binary (all header variants 401) — found via full driver repro in an env-checkpoint box. **Workers now use the CLI path** (`opencode run --standalone --session <ses-id> --auto --format json`, PWD pinned to the repo) — the same V-E1-verified invocation the eval's main agents ran all night, plus a live in-box verification: agent ran (1998/158 tokens), edited product code, missing-summary correctly errored | ✅ CLI path verified live in-box 2026-11-02; first full-episode verification = the relay2 re-run |
| V-oc-1 (V1-V5) | OpenCode plugin event names, client message fetch, custom tool registration, toast, `noReply` | install OpenCode in a box + live session |
| V-cx-1 (V16) | `codex exec --json` framing with a Sail custom provider | run pinned Codex in a box |
| V-rt-4 | `X-SailboxId` header actually sleeps boxes during inference | measure box sleep behavior |
| V-rt-6 | Egress restrictions; scoped API keys | policy API probe |
| ~~V-E2~~ | **VERIFIED 2026-11-01 — see the OpenCode CLI table above.** | |
| ~~V-oc-1 (V1-V5)~~ | **VERIFIED 2026-11-01 — see the OpenCode v2 plugin table above.** | |

## Adaptations made from the design

- **Pi/OpenCode/Codex install channels are unknown** (not covered by the
  design's pinned-version list). The base image installs only the common
  toolchain; harness CLI install commands are a config knob to fill at
  M0. Recorded as a paper-cut candidate.
- `Sailbox.from_checkpoint` does not accept `max_lifetime_seconds` in its
  signature → exploration boxes rely on the reaper + driver budget for
  cleanup when created from checkpoints.
