# Pre-registration: four setups on SlopCodeBench

Written before any of these runs. Date: 2026-11-04. Replaces `PREREG_slopcode_pairs.md`,
whose comparison was stopped and is not a result. This is a first read and not a
definitive test: ten tasks can only show a very large effect.

## Setups, all started together
- A. The agent alone.
- B. Hindsight, forced at each task's middle round, rounded up. The next request waits.
- C. Hindsight when the detector says yes. Requests arrive on the clock, with no waiting.
- D. Hindsight when the detector says yes. The next request waits.

Waiting means that while a background run is active, the next request is held until the
run ends, up to 4 hours, and each hold is recorded. A held request is not what a real
user would do. C shows what happens without that help.

## Tasks
The 26 tasks with clean reference solutions, excluding the 4 used for tuning, were
narrowed to the 15 with 4 or 5 rounds. Three-round tasks leave too little after a design,
and longer tasks would not finish in about 7 hours. Ten were drawn with
`random.Random(20261104).sample(sorted(tasks), 10)`:
code_search, etl_pipeline, file_merger, l2m, layered_config_synthesizer, log_query,
metric_transform_lang, migrate_configs, pwd_manager, xjq.

These tasks were seen in the stopped comparison, and prompts have changed since.

## Fixed for all
- Model `zai-org/GLM-5.3-Flash` in every role. One run per task per setup.
- The simulated user agrees to what the agent recommends.
- No time cutoff. Code frozen at the commit that adds this file. A fix made during the
  runs is recorded, and affected runs are rerun or dropped.

## What is compared
Main comparison: B against A. Also D against A and C against A, and D against B.
Paired by task, on the last checkpoint scored in both setups:
1. Erosion and verbosity without test files (lower is better).
2. Isolated, strict and regression pass rates, averaged over checkpoints scored in both.

Reported with median difference, win/loss/tie count and a sign test. With 10 pairs, a
two-sided sign test at .05 needs 9 wins and no ties. There are several comparisons and
outcomes, so one small p-value alone is not a finding.

## How it is read
- Every task with both setups scored counts, including tasks that never got a design.
- Tasks where a design was delivered or used are reported as exploratory.
- A task with an infrastructure failure in either setup is dropped from that comparison
  and listed. Time asleep is recorded, and a task that slept over 60 s is flagged.
- Scores can't be compared with published leaderboards.

## Also reported
Cost per setup, runs started, designs delivered, how late they were, what the agent took,
holds, and time spent waiting for a box slot.

## Stop rule
Stop launching work and ask the user if total estimated spend nears $250. The user's
overall limit is $550.

## Recorded changes during the runs
- 2026-11-04, 19:27. B, C and D were stopped about 3.5 hours in, after 53 workers failed to
  get a box: the organization's limit counts reserved vCPUs, 8 per box by default, or about
  25 boxes. Nothing from those three setups counts. Restarted from the commit adding this
  note: Hindsight's boxes reserve 1 vCPU and main agents' boxes 4, and every box creation
  waits for a slot, up to 90 minutes.
- A was hardly affected and is kept. It started about 3.5 hours earlier than B, C and D, on
  8-vCPU boxes. In a sample of live boxes, usage ranged from about 0.02 to 1 vCPU, so we
  don't expect the size to matter, but this is a difference between the setups.
- The $250 stop point was lifted by the user. Their overall limit stays $550.
- 2026-11-05. Two D runs, on `file_merger` and `log_query`, crashed in review: a worker's
  summary listed what it addressed as dicts, and formatting it raised a `TypeError`. The run
  was never closed, so D's waits ran their full 4 hours. `file_merger` ended on its own, and
  the `log_query` run was closed by hand, with the reason recorded, so the task could finish.
  Neither delivered, and they count as lost designs for D, not as infrastructure failures.
  Fixed after the runs: odd summaries are formatted safely, and an error in review ends the
  run.
- `file_merger` hidden tests did not run in A (rounds 2-4), C (round 4) or D (rounds 2-4):
  pytest in the box got "Permission denied" on `/dev/null`. Cause not confirmed. Per the plan,
  the task is dropped from every comparison, leaving nine pairs.

## Result
Every comparison is nine pairs. Counts are tasks where the first setup did better, worse or
tied, on erosion and verbosity without test files at the last round scored in both, and on
strict, isolated and regression pass rates averaged over rounds scored in both.

| Comparison | Erosion | Verbosity | Strict | Isolated | Regression |
|---|---|---|---|---|---|
| B vs A | 5/4/0 | 5/4/0 | 4/5/0 | 4/5/0 | 5/2/2 |
| C vs A | 5/4/0 | 7/2/0 | 2/6/1 | 3/5/1 | 1/5/3 |
| D vs A | 4/5/0 | 7/2/0 | 4/5/0 | 5/4/0 | 4/4/1 |
| D vs B | 6/3/0 | 6/3/0 | 3/6/0 | 3/6/0 | 2/3/4 |

No comparison reached significance (the smallest sign-test p was .18). Medians for B vs A
were -.031 erosion, -.003 verbosity and -.001 strict pass rate. D's 7/2 on verbosity was
already 7/2 after round 1, before any design, so it is not an effect of Hindsight.

Also reported: designs delivered B 7, C 6, D 10. Reading the agent's replies, it took a whole
design about 6 times, part of one about 11 times, and only tests or nothing about 6 times.
Requests were held 15.5 hours in total in B and 34.2 in D (longest 4 hours, from the crash).
No box slot waits. Estimated cost at placeholder prices: A $24, B $53, C $51, D $98, not
checked against the bill. `log_query` and `file_merger` in D slept 614 s and 10,333 s, so
their timing is flagged.
