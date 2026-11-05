# Pre-registration: what should background workers be aimed at?

Written 2026-11-02, before any result from this experiment exists.

## Hypothesis

Workers given the user's verbatim requests, with no edge tests, fix graded
failures that edge-hunting workers do not. Today's workers are told edge tests
"are why you are here", so they chase what the edge-test model imagined.

## Design

`eval/lab/proposal_lab.py`. Start from final repos of `baseline_alone` episodes
and run one worker per arm from the same code, with no main agent and no fleet.
Hidden tests are uploaded only after the worker has finished, and scored on both
the untouched repo and the worker's result.

| arm | the worker is told |
|---|---|
| `reqs_audit` | the verbatim requests: audit the code against each, fix mismatches, invent nothing |
| `edge_hunt` | the same requests, but find up to 5 failing edge cases and fix them: a proxy for today's workers, not the real pipeline |
| `fresh_rebuild` | the same requests, starting from an empty repo |

## Metrics

Unit: one worker run on one checkpoint.

- **Rescue:** a checkpoint failing hidden tests passes all of them afterwards.
- **Improved:** more hidden tests pass than before.
- **Harm:** a checkpoint passing all hidden tests no longer does.

## Decision rule

Adopt requirement-driven workers (`reqs_audit`) as the default only if all hold:

1. Rescue rate at least 50% on failing checkpoints.
2. Rescue rate at least double `edge_hunt`'s.
3. On failing checkpoints where the two arms differ, `reqs_audit` wins more, by
   an exact sign test at p < 0.05 if there are enough discordant pairs to reach
   it. If there are too few, say so and do not claim significance.
4. Harm no higher than `edge_hunt`'s on passing checkpoints.

If 1 fails but `fresh_rebuild` meets it, the finding is that rebuilding beats
auditing. If every arm fails, aiming the workers differently is not enough.

## Known limits

- Checkpoints are clustered by task, so a single task can dominate. Report results
  per task as well as pooled.
- `edge_hunt` is a proxy. It shares the requests, so any gap understates the cost
  of relying on the model-written ledger.
- This measures patch quality only. It says nothing about whether a main agent
  would use the patch.
- If calibration yields too few failing checkpoints, this cannot conclude. Say so
  rather than widen the criteria.

## Result of lab1 (2026-11-02)

96 of 96 runs usable. Rescue on failing checkpoints: `reqs_audit` 10/14 (71%),
`edge_hunt` 5/14 (36%), `fresh_rebuild` 13/14 (93%). Harm on passing checkpoints:
0/18 in every arm.

Against the rule: (1) met; (2) met, exactly double; (3) 5 wins, 0 losses, p=0.0625.
Five discordant pairs cannot reach p<0.05, so significance is NOT claimed; (4) met.
The rule's adoption condition is met, without statistical significance.

`fresh_rebuild` was not the comparison the rule was written for. Its higher rate is
noted as post hoc, and a rebuild may not transfer to larger repos.

Defect: patches were lost in every repo with a `.gitignore`, so patch sizes from
lab1 are invalid. Scores are unaffected because they run on the repo itself. (This
first blamed agents committing their work. That was wrong; see lab2 below.)

## Addendum: lab2, written before it runs

Re-run `reqs_audit` only, with the capture fixed, 2 reps per checkpoint, same
checkpoints as lab1.

Question: does it leave correct code alone? Measure on passing checkpoints: the
share of runs whose diff changes non-test files.

- At most 25% of runs: "change nothing if correct" is workable as the no-op signal.
- More than 25%: the mode needs a separate gate before it can deliver anything.

Also report the rescue rate on failing checkpoints as an independent replication.
Pooling with lab1 is allowed for the rescue rate only.

## Result of lab2, and lab3 written before it runs

Lab2 cannot answer its question: its patches were empty too. Diagnosis in live
boxes: `git add -- ':(exclude)__pycache__'` exits 1 when `__pycache__` is ignored,
and the capture chained on it. Agents had not committed anything. "0 of 18 changed
non-test files" is an artifact and must not be cited.

A no-agent control on meetings r3 scored 2/2/2 across base, repo and base again, so
scores did not move on their own. Lab2's rescue count stands: 9/14, with 4/4
bookmarks, 1/4 events, 4/6 meetings.

Lab3 repeats lab2 with capture fixed, plus a git-independent list of changed files
that fails the run if it disagrees with the patch. Same question, same 25% threshold,
same checkpoints, same counting of non-test files. Any change to README counts as
non-test. Rescue may be pooled with lab1 and lab2.

## Result of lab3 (2026-11-02)

32 of 32 runs usable. On passing checkpoints, 1 of 18 runs changed non-test files
(6%; Wilson 95% CI 1%-26%), under the 25% threshold, though the interval's top
edge just crosses it. No run broke a hidden test. "Change nothing if correct" is
workable as the no-op signal, judged on product code: 17 of those 18 runs were
close (16 added tests, 1 changed nothing).

The one exception, cart r0 rep 1, rewrote `cart.py` and the README so that `add`
appends lines instead of merging. It still passes the hidden tests. So a worker can
make an unrequested design change that no hidden test catches.

Rescue on failing checkpoints: 11/14. Pooled across lab1, lab2, lab3: 30/42 (71%,
Wilson 56%-83%). Those runs are not independent: 7 checkpoints, 3 tasks. By task
in lab3: bookmarks 4/4, events 2/4, meetings 5/6. All 11 rescues changed product
code and all 3 misses did not, so silence means "the worker found nothing", not
"the code is correct".
