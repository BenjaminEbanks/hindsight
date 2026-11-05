# Hindsight: design

The single design document for this project. Last updated 2026-11-04.

## The idea

A coding agent builds one request at a time, so its early decisions are made without
knowing what comes later. It tends to fit each new request in with the smallest change
and rarely revisits those decisions.

Hindsight watches an OpenCode session. When a request changes something that held
before, background workers in Sailboxes rebuild the code knowing everything the user has
asked for. The result goes back to the main agent as a suggestion.

| | Main agent | Background workers |
|---|---|---|
| Strengths | Talks to the user; has their tools and data; knows the latest code | See the whole conversation at once; can try several designs in parallel |
| Weaknesses | Anchored on earlier decisions | Can't talk to the user; can fall behind the conversation |

The design plays to both. Workers supply the hindsight. The main agent and the user
decide what, if anything, to use.

Whether the suggestions improve the code is **not yet shown**. See [Evaluation](#evaluation).

## How it works

```
 User message -> main agent works -> turn ends
                                       |
                 1. Record: save messages, snapshot the code
                                       |
                 2. Detect: did the requirements change?  -- no --> wait for next turn
                                       | yes
                 3. The auditor names 2-5 directions. One redesign worker each,
                    plus one with none (new user messages reach them as files)
                                       |
                 4. Free checks: tests not weakened? product code changed?
                                       |
                 5. 3 critics, then a chairman picks a design -- nothing better --> send nothing
                                       |
                 6. Final worker finishes that design, from its code
                                       |
                 7. Final check: tested on the code it was built on, one retry
                                       |
                 8. Deliver to the main agent at the next gap between turns
                                       |
                 9. Agent reviews and recommends -> user decides -> agent acts
```

1. **Record.** Each turn saves the user's messages and snapshots the folder to a private
   git repository, separate from the user's own. A snapshot is usable once the files have
   settled, no merge or rebase is in progress, and the turn wasn't aborted.
2. **Detect.** From the second message on, a model reads the earlier requests, the latest
   request, the code and past runs, and answers one question: does the latest request
   change something that held before? It does not read replies, tool calls or test
   results. Detection is skipped while the session has a run.
3. **Workers.** After a yes, the auditor reads the requests and code again and names 2 to
   5 structurally different directions. Each gets a worker, and one more worker gets none,
   to cover what the auditor missed. So a run normally has 3 to 6 workers. If choosing
   directions fails, only the open worker runs. The user's verbatim messages are their
   specification, and anything they fix, such as a named file, stays fixed. Every worker
   must build a redesign and say what it makes better and what it costs. A dropped
   connection continues the worker's conversation. At 80% of its time a worker is told to
   wrap up, and if it still runs out, its code is kept and labelled as unfinished.
4. **Free checks.** A design that weakens tests or changes no product code is dropped.
   Comments, docstrings, project metadata and prose don't count as product code, and
   installed dependencies never enter a patch. No model or box is used.
5. **Council.** Critics review the designs anonymously, reading the current code and each
   design's product code without its tests, up to a size limit that says what it cut. A
   chairman picks one design, plans what to change in it, borrowing from the others, or
   decides that no design is worth sending and ends the run. A lone design goes through
   this too.
6. **Final worker.** Starts with the chosen design's code in place, on the base that design
   was built on, and makes the plan's changes. New messages arrive as files it reads
   before finishing, and the newest messages win. Once it has finished it isn't sent back:
   later messages are listed for the main agent instead.
7. **Final check.** Runs the project's tests on that same base code, with and without the
   design. A regression gets one retry: a new worker starts from that design, with the
   failures. Otherwise the run ends with nothing sent. This shows only that it broke
   nothing checked, not that it is better.
8. **Deliver.** At the first gap between turns, the main agent gets:
   - one sentence written from checked facts, plus how the design was checked;
   - a statement that this is a suggestion: use all of it, part of it, or only its ideas,
     weighing structure as well as behavior;
   - the engineers' own description and the chairman's plan, labelled unverified;
   - any user messages the design hasn't seen, and how many messages behind it is;
   - the diff, cut at 24,000 characters, with a pointer to the full text and the design's ID.

   In OpenCode Desktop the user also gets a toast, with buttons to review, adopt or dismiss.
   Elsewhere they hear through the main agent.
9. **Decide.** The agent tells the user what it recommends and changes nothing until they
   agree. It can question the worker that built the design through `hindsight_ask`. That
   worker's box sleeps after delivery and wakes on a question.

One run at a time per session. A failed run doesn't block the next one, and a run that
ends without a design leaves detection free to fire again.

## Key decisions

### Detection

| Decision | Why | Replaced |
|---|---|---|
| Detect changed requirements only | It can be checked against the requests, while "would you structure this differently?" is a matter of taste and fits almost any code | Asking whether the code needs a redesign. On Flash that said no on every live review across 4 SlopCodeBench tasks, and looked like it stopped at "the code already works" |
| Whether the agent already handled the change doesn't matter | Hindsight's value is in the case where it was made to work the quick way | "Judge the code as it stands" |
| Small edits don't count | A renamed field shouldn't start six workers | Unchanged |
| A model reads requests and code directly | The ledger paraphrased the user's words, a rewording counted as a change, and most of its fields had no readers. It also scored low on a small synthetic set (recall 0.78, precision 0.24, F1 0.37) | Requirements ledger, drift score, edge-case search, dissatisfaction detector |
| No minimum turn or quiet period | Fixed thresholds were guesses. Early firing has the longest payoff, and a gate at turn 4 or 5 leaves little to measure on a short task. Revisit with data | Minimum turns, idle timers |

### Workers and selection

| Decision | Why | Replaced |
|---|---|---|
| The auditor chooses the directions, plus one open worker | Parallel workers are best spent on different structures, which depend on the code. The count follows from how many real alternatives exist. The open worker covers the auditor's blind spots. Not yet shown to work | Six fixed roles, whose prompts mostly asked for audits; difficulty-scaled workers, which only sized the team |
| Every worker must redesign | Told that leaving the code alone was valid, 28 of 45 designs changed no real code, and those we read kept the same structure | Workers deciding whether to change anything |
| Critics and the chairman can decide to send nothing | Workers can no longer say no, so the choice moves to where the designs can be compared with the current code | Workers saying the design fits |
| The spec is the user's verbatim words; no invented requirements | Workers told that edge tests "are why you are here" chase what a model imagined. In lab tests, audit workers given the requests rescued 30 of 42 failing checkpoints (not independent: 7 checkpoints, 3 tasks), against 5 of 14 aimed at edge tests. Redesign workers haven't had this test | Edge tests, ledger as spec, hidden tests |
| One finishing worker continues the chosen design's code | Rebuilding from a text plan flattened designs into moved code, and 3 of 4 final workers ran out of time or lost their connection doing it. Not yet shown to fix this | Pairwise judge, port worker, delivering the best individual design, final worker building from the plan alone |
| Critics read whole designs and the current code | Seeing the first 6,000 characters of a patch, they couldn't judge structure | Patch excerpts |
| Out of time keeps the code, labelled | Time-limited work was thrown away: 5 of 21 workers in one trial | Refusing the worker |
| Run only what's about to be sent | Only the design that gets sent needs to pass. Worker test results still go to the critics as information | Testing every design |
| A checked final design or nothing | What gets sent has always passed the same check, and an empty run says so plainly | Extra workers on failure, fallback picks |

### Staleness and delivery

| Decision | Why | Replaced |
|---|---|---|
| Don't merge onto the latest code | A merge conflict threw away a design whose ideas were still useful. It says the code moved, not that the design is wrong | `git apply --3way` rebase, conflict retries |
| Deliver stale designs and say how far behind | The main agent knows the current code, so it can reconcile. Dropping one on a conflict lost its ideas, though that design was weak. A separate fresh note was rejected because nothing in it could be checked from the agent's repo | Dropping stale results |
| Test on the code it was built on | That's the only base where the result is clean. The agent is told it wasn't tested on current code | Testing on the latest code |
| Send to the main agent, with the diff | A text-only note left the agent unable to check the evidence | Desktop toast to the user only |
| A suggestion, not a patch to apply | The agent may use only the ideas, written its own way | Adopt-or-dismiss |
| Include the engineers' reasoning, labelled unverified | A one-sentence summary and a large diff left the agent guessing the intent. This relaxes the old "never worker prose" rule, and the agent can check the words against the diff | Facts only |
| Deliver at turn gaps, no nudge limit | Messages are stored when a turn ends, so that's when "what hasn't it seen?" can be trusted. One run at a time already paces delivery | Idle timers, rate limit, 3-nudge cap |
| The agent recommends, the user decides | Otherwise the agent might act before the user chooses | Unchanged |

## Trust and safety rules

- **Only the adoption engine among Hindsight's code touches the user's repo**, on an
  explicit command, with a backup, a test run and one-command undo. The main agent may also
  edit, as it always can, once the user agrees.
- **The real API key never enters a box.** A saved egress policy adds it to model calls
  only (`POST /v1/chat/completions`); boxes hold a placeholder. If a box can't get the
  policy, it's closed, and `hindsight doctor` checks that it exists. The old synthetic
  runner, `scripts/live_eval_swarm.py`, is the one exception, and refuses to run without
  an explicit flag.
- **Worker output is untrusted.** Its tests aren't accepted as proof, and test changes
  must be listed with a reason for the main agent to judge.
- **Secrets are scanned before upload**, and obvious secret files are excluded.
- **Boxes are closed by the run that made them.** Nothing ends boxes account-wide.
- **Never fake results.** Mocks stay in labelled unit tests, and every decision is logged
  with its reason.

## Where things live

| Area | Files |
|---|---|
| Wiring and per-turn flow | `hindsight/orchestrator.py` |
| Session events, snapshots | `hindsight/sources/`, `hindsight/snapshot.py` |
| Storage | `hindsight/store.py` (SQLite) |
| Detection | `hindsight/design_review.py`, `hindsight/session_gate.py`, `prompts/design_review.txt` |
| Workers | `hindsight/runs.py`, `hindsight/executor.py`, `hindsight/box/driver.py`, `prompts/worker_preamble_audit.txt` |
| Checks and council | `hindsight/gate/integrity.py`, `hindsight/requirement_audit.py`, `hindsight/selection.py`, `hindsight/council.py` |
| Delivery and follow-up | `hindsight/delivery.py`, `hindsight/resume.py`, `hindsight/adopt.py` |
| OpenCode plugin | `plugin/hindsight/` (`hindsight_status`, `_show`, `_ask`, `_adopt`, `_undo`, `_dismiss`, `_explore`) |
| Sail access | `hindsight/sail_client.py`, the only path to Sail |
| Model calls and keys | `hindsight/models.py` (Sail or any OpenAI-compatible provider), `hindsight/box_key.py` |
| Costs and cleanup | `hindsight/budget.py`, `hindsight/reaper.py` |

The coordinator runs on the user's machine for now, with a clean boundary so it can move
to a Sailbox. Workers keep running in their boxes, but they need the coordinator to
finish. A Sailbox coordinator would need the real key, which the key rule above must
account for.

## Default settings

| Setting | Default |
|---|---|
| Worker, final worker | `zai-org/GLM-5.3` |
| Detector, auditor | `moonshotai/Kimi-K3` |
| Critics, chairman | `moonshotai/Kimi-K3` (a different family, intended to reduce self-preference; benchmark runs so far used one model throughout, so this is untested) |
| Delivery sentence | `zai-org/GLM-5.3-Flash` |
| Critics per run | 3 (0 turns them off) |
| Worker time limit | 45 minutes |
| Budget | $15 per session, $40 per day |
| Messages a design hasn't seen | `list_missed` (`retry_final` sends it back first) |
| Sleeping box lifetime | 24 hours |

## Open questions

| Question | Notes |
|---|---|
| Do small requirement changes justify six workers? | In a replay of 40 saved reviews it said yes on 19, and about 5 or 6 looked small. Left in to see what those runs produce |
| Should there be a minimum turn? | Decide from which turns produce useful designs |
| Limit repeated failed runs? | Nothing stops a run after every run has failed |
| Re-ask a final worker that replies with nothing? | It currently ends the run with nothing sent |
| How should adoption handle a stale patch? | `hindsight_adopt` applies the diff directly, so it may fail, and the agent would then make the edits itself |
| Does `hindsight_show` work by ID from a delivery? | Every delivery now includes the ID, but this hasn't been run through the real plugin |
| Should the box agent stop running as root? | It still does, because images are built around root. The key policy limits the damage |

## Evaluation

No result yet shows whether nudges improve the code. The current comparison's rules are in
`eval/lab/PREREG_four_setups.md`, and `eval/README.md` lists the others. In the benchmark, the
simulated user can hold the next request until a running redesign ends, to test whether a design
helps when it arrives in time, and each hold is recorded to show how often a design would have
been late. Real users wouldn't wait.

## Related records

- `docs/VERIFY.md`: external API behaviour checked live.
- `docs/PAPERCUTS.md`: integration friction, logged as it happens.
- `eval/`: evaluation tasks, past reports and experiment records.
- Git history: the full reasoning behind removed designs.
