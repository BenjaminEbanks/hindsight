# Pre-registration: can a read-only reviewer decide when to call background agents?

Written 2026-11-02, before any result exists.

## Question

Can a model that only reads the session and the code tell repos that fail hidden
tests from repos that pass them? No running code, no tools, no hidden tests.

## Design

`eval/lab/reviewer_lab.py`. Final repos of the 54 `baseline_alone` episodes in
`calib1`: 7 failing, 47 passing. 3 reps per repo, per definition, per model.

Definitions:
- `broad`: the code does not do what the user asked, or the latest request means an
  earlier decision should change.
- `strict`: there is a design-level problem that a local fix would not solve.

Models: GLM-5.3-Flash (cheapest) and Kimi-K3 (the judge).

## Measures

- Catch rate: share of reviews saying yes on failing repos.
- False-yes rate, on passing repos in the same 3 tasks that have failures. This is
  the primary comparison: other tasks differ in difficulty.
- False-yes rate on the other 6 tasks, reported separately.
- Per-task results, and seconds per decision.

## Decision rule, for `broad`

For each model:
- **Works:** catch rate at least 70% and same-task false-yes rate at most 25%.
- **Can't tell them apart:** catch rate minus same-task false-yes rate is under 20
  points. Reading alone is not enough, so reconsider an auditor that runs things.
- **In between:** usable only with a stated trade-off. Report it, adopt nothing yet.

`strict` is reported for comparison only. Failing repos here were fixed by small
patches, so a low `strict` catch rate is expected and is not a failure.

## Known limits

- 7 failing repos from 3 tasks, so reps of one repo are not independent. Results are
  a screen, not a measurement of real traffic.
- Final repos only, not turn by turn: the reviewer sees the finished state.
- Hidden-test failure is the only label, so a real problem hidden tests miss counts
  as a false yes.
- No judge or prompt tuning after seeing results. Any retry is a new, labelled run.

## Result of rev1 (2026-11-02)

648 reviews, all with a verdict.

| Model / definition | Caught failing | False yes, same tasks | False yes, other tasks |
|---|---|---|---|
| Flash / broad | 0/21 | 0/69 | 0/72 |
| Kimi / broad | 3/21 (14%) | 1/69 (1%) | 0/72 |
| Flash / strict | 0/21 | 0/69 | 0/72 |
| Kimi / strict | 0/21 | 0/69 | 0/72 |

Against the rule: both models fall under "can't tell them apart" (0 and 13 points,
under 20). A read-only reviewer with this prompt is not enough.

All 3 catches were on bookmarks, and their reasons name the real bug: old files hold
`{title, url}` objects, and `load()` only accepts arrays. The reviewer was accurate
when it said yes, and rarely said yes. Kimi caught nothing on events or meetings.

Limits on this null: the prompt has "do not require it" and "focus on the latest
request", which may push towards no. These failures have small fixes, so this does
not test whether a reviewer can spot design-level problems: the set has none that
we know of. Hidden tests are the only label.
