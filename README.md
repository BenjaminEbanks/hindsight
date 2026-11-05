# Hindsight

Background agents for [OpenCode](https://opencode.ai) that watch your coding session and
bring redesigns back to your agent.

The paper, [*Is Hindsight Really 20/20?*](paper/Is-Hindsight-Really-20-20.pdf), explains the thinking and what we've
learned so far.

## The problem

A coding agent builds one request at a time. Its early decisions are made without knowing
what you'll ask for later, and when a new request changes something those decisions relied
on, it tends to fit the change in with the smallest edit it can. Each edit is reasonable, and
the structure gradually stops suiting the code.

Your agent could stop and rethink, but that would interrupt you, and it is already anchored
on what it built. Hindsight does the rethinking in the background. Workers see everything
you've asked for at once and can try several structures in parallel. Your agent then reviews
the result, and you decide what to use.

## How it works

```
 1. Monitoring     You work in OpenCode as usual. After each turn, a detector asks:
    and detection  did this request change something the code was built around?
                                   | yes
 2. Exploration    An auditor names 2-5 directions. One worker builds each, and one more
                   gets an open brief. Each runs in its own Sailbox with its own copy of
                   the code. Automatic checks reject weakened tests and cosmetic changes.
                                   |
 3. Review         Three critics read the designs, and a chairman picks one or decides
                   nothing is worth sending. A final worker finishes the chosen design.
                                   |
 4. Delivery       At the next gap between turns, the design reaches your agent, and you
    and decision   get a toast. Your agent recommends all, part or none. You decide.
```

Your session never waits for any of this. Nothing writes to your repository except the
adoption tool, which you have to approve, and which takes a backup and offers undo.

## Setup

You'll need:

- [OpenCode](https://opencode.ai) (developed against 2.2.5), [uv](https://docs.astral.sh/uv/)
  and git
- A [Sail](https://docs.sailresearch.com) account, for the boxes workers run in
- A model provider with an OpenAI-compatible API. This can be Sail too.

**1. Install Hindsight and set your keys**

```bash
git clone https://github.com/BenjaminEbanks/hindsight.git ~/hindsight
cd ~/hindsight && uv sync

export SAIL_API_KEY=...         # runs the boxes
export OPENROUTER_API_KEY=...   # only if your models aren't on Sail; use your provider's name
```

**2. Add it to your project**

```bash
cd ~/your-project
uv run --project ~/hindsight hindsight init
```

This writes `.hindsight.toml`, installs the plugin into `.opencode/plugins/`, and installs the
toast part into `~/.config/opencode/plugins/`. Your existing OpenCode setup stays as it is.

**3. Choose your models and test command** in `.hindsight.toml` ([details below](#use-your-own-models)).

**4. Let workers reach your models without holding your key**

```bash
uv run --project ~/hindsight hindsight box-key    # stores the key in Sail, saves the policy
uv run --project ~/hindsight hindsight doctor     # checks everything and says how to fix it
```

**5. Work as usual.** Leave the coordinator running in one terminal, and use OpenCode in another:

```bash
uv run --project ~/hindsight hindsight up
opencode
```

## Use your own models

There are three separate choices.

| What | Where it's set | Key |
|---|---|---|
| The agent you talk to | Your normal OpenCode config. Hindsight doesn't change it. | Your OpenCode setup |
| Hindsight's models | `[models]` in `.hindsight.toml` | `api_key_env` |
| The boxes workers run in | Always Sail | `SAIL_API_KEY` |

By default, Hindsight's models come from Sail inference, so `SAIL_API_KEY` covers everything.
To use another provider, set its details and pick a model for each role:

```toml
[models]
provider    = "openrouter"                     # any label
base_url    = "https://openrouter.ai/api/v1"
api_key_env = "OPENROUTER_API_KEY"             # the variable's name, never the key itself

worker   = "your-provider/strong-coding-model"   # workers and the final worker
reviewer = "your-provider/strong-model"          # detector and auditor
judge    = "another-family/strong-model"         # critics and chairman
nudge    = "your-provider/cheap-fast-model"      # one-sentence summary of a delivery

[models.prices]                                  # USD per million tokens, for the budget
"strong-coding-model" = [0.60, 2.20]             # matched on part of the model's name
```

Then run `hindsight box-key` again. Policies can't be edited, so if you've run it for a
different provider before, also set `box_policy` to a new name.

What to know about providers:

- **It needs chat completions at `<base_url>/chat/completions`, with a bearer token.** Providers
  that only offer another format, or authenticate differently, need a proxy in front of them.
- **The URL must be public HTTPS.** Workers call it from their boxes, so a model on `localhost`
  won't work.
- **Some newer models reject `max_tokens`.** If yours does, set
  `max_tokens_param = "max_completion_tokens"`.
- **All our testing used GLM and Kimi models through Sail.** Other providers have only been
  tested against a fake server, and prompts may behave differently on other models.

### How your keys are handled

- Keys stay in environment variables on your machine, and `.hindsight.toml` only names them.
- Boxes never hold your model key. `hindsight box-key` stores it as a Sail secret, and an egress
  policy adds it only to chat-completion requests to your provider. Workers see a placeholder.
- This means **your model key is stored in your Sail account**.
- Your code is copied to Sailboxes, apart from files matching `[project].exclude` and a built-in
  list such as `.env*` and `*.pem`. Check these before using Hindsight on a private project.

### Other settings

`.hindsight.toml` also controls the budget (default $15 per session, $40 per day, estimated
from token counts), the worker time limit, how many workers and critics run, and more.
[`.hindsight.example.toml`](.hindsight.example.toml) lists the common ones, and
[`hindsight/config.py`](hindsight/config.py) has all of them. A run costs real money: our most
recent runs cost about $2 to $4 each using one cheap model.

## Commands

| Command | What it does |
|---|---|
| `hindsight init` | Adds Hindsight to a project |
| `hindsight box-key` | Sets up model access for worker boxes |
| `hindsight doctor` | Checks every integration |
| `hindsight up` | Starts the coordinator on port 8787, one project at a time |
| `hindsight status`, `trace` | Shows what's running, or one session's timeline |
| `hindsight inbox`, `show`, `ask` | Lists designs, shows one, or asks its author a question |
| `hindsight adopt`, `undo` | Applies a design, or reverts it |

Your agent gets the same abilities as tools in OpenCode: `hindsight_status`, `_show`, `_ask`,
`_adopt`, `_undo`, `_dismiss` and `_explore`.

## Where to read the code

| Stage in the paper | Code |
|---|---|
| Wiring, and what happens each turn | [`hindsight/orchestrator.py`](hindsight/orchestrator.py) |
| 1. Watching the session | [`plugin/hindsight/`](plugin/hindsight/), [`hindsight/api.py`](hindsight/api.py), [`hindsight/snapshot.py`](hindsight/snapshot.py) |
| 1. Detection | [`hindsight/design_review.py`](hindsight/design_review.py), [`hindsight/session_gate.py`](hindsight/session_gate.py) |
| 2. Workers | [`hindsight/runs.py`](hindsight/runs.py), [`hindsight/executor.py`](hindsight/executor.py), [`hindsight/box/driver.py`](hindsight/box/driver.py) (runs inside each box) |
| 2. Automatic checks | [`hindsight/gate/integrity.py`](hindsight/gate/integrity.py), [`hindsight/selection.py`](hindsight/selection.py) |
| 3. Critics and chairman | [`hindsight/council.py`](hindsight/council.py) |
| 4. Delivery and adoption | [`hindsight/delivery.py`](hindsight/delivery.py), [`hindsight/resume.py`](hindsight/resume.py), [`hindsight/adopt.py`](hindsight/adopt.py) |
| Model calls and keys | [`hindsight/models.py`](hindsight/models.py), [`hindsight/box_key.py`](hindsight/box_key.py), [`hindsight/sail_client.py`](hindsight/sail_client.py) |
| All prompts | [`hindsight/prompts/`](hindsight/prompts/) |

The rest of the repo:

| Folder | What's in it |
|---|---|
| [`paper/`](paper/) | The paper, its figures and the data behind them |
| [`docs/DESIGN.md`](docs/DESIGN.md) | The full design, its decisions and open questions |
| [`docs/VERIFY.md`](docs/VERIFY.md), [`docs/PAPERCUTS.md`](docs/PAPERCUTS.md) | Dated logs of API behaviour we checked, and integration problems we hit |
| [`eval/`](eval/) | Evaluation tasks and pre-registered experiments |
| [`scripts/`](scripts/) | Evaluation runners |
| [`tests/`](tests/) | Unit tests, and live contract tests |

## Tests

```bash
uv run pytest
```

This runs the unit tests, which use fakes and need no keys or network. The tests in
`tests/contract/` create real boxes and spend real credits, so they only run if you name them.

## Evaluation

[`eval/README.md`](eval/README.md) lists the experiments and how to run them. Benchmark data and
run transcripts aren't in this repo: the benchmarks have their own licenses, and transcripts can
contain anything an agent read.

## Build the paper

From `paper/`, run `latexmk -pdf main.tex` or `tectonic main.tex`.
