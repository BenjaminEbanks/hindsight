# Eval set v2 — incremental design breaks with realistic users

Second-generation staged tasks. Each is a believable project story where a
casually-stated requirement invalidates the design the agent was licensed to
build. Same schema and the same discrimination bar as v1 (`eval/tasks/`):
hidden tests pass on the reference final implementation and fail on the naive
stage-1 design — verified in `tests/unit/test_eval_tasks_v2.py`.

## Spec revision (2026-11-02)

Stage messages and hidden tests were corrected so every tested contract is
stated in text the agent sees. Results from before this revision are not
comparable. The closing turn lives in the runner, not in these files.

- `meetings`: `list()` return shape and the `next_dates` start date are stated.
- `bookmarks`: `test_current_files_still_load` hardcoded a file format the spec
  leaves open; replaced by `test_migrated_files_survive_another_save`.
- `expenses`, `cache`, `metrics`, `notify`: stated empty-category, missing-key,
  unknown-counter, no-id, sender-signature and final-error behaviour.
- `events`: tests pass a `str` path, since the spec never promises `Path`.

## The tasks

| id | story (who / what turns) | archetype | register / staged phenomena | break |
|---|---|---|---|---|
| `meetings` | team calendar; two hires in Berlin, then a recurring standup | naive local strings → tz-aware → recurring wall time (the only double break: the correct-looking stage-2 fix, UTC instants, is itself broken by stage 3) | polite; **praise wrapping the break** (dissatisfaction must stay false) | 2, 3 |
| `expenses` | monthly-report expense log; finance asks to see behind the numbers, then to take purchases back | derived-only state → source of truth (data-model inversion) | neutral; **anti-supersede sentence** ("record() and total() stay as they are") | 2 |
| `cache` | service cache; traffic becomes threaded, then the cache must be bounded | unsynchronized miss → single-flight per key → LRU | terse/IM, all-lowercase updates | 2 |
| `metrics` | tiny metrics module; a test harness and then Prometheus arrive | module-global singleton → isolated instances → labeled series | **non_goal reversal** ("I know I said no class needed, but…") | 2, 3 |
| `notify` | notifier over a pluggable sender; a redelivering queue, then a flaky gateway | at-most-once → at-least-once with idempotency (only a successful send marks done) | neutral; **paraphrased restatement** + anti-supersede | 2, 3 |
| `events` | log-file reader; files get huge, then a filter is wanted | eager list → lazy stream (observable only through a counting file-like) | question form ("Can we get…") | 2, 3 |
| `bookmarks` | bookmark store; folders arrive, then beta users' old files surface | unversioned JSON → schema migration (versioned writes, migratable reads) | grumbly ("Ugh, of course…") — venting must not register as dissatisfaction | 3 |
| `importer` | row importer with a callback; users navigate away mid-import | fire-and-forget → cancellable, restartable, observable (the trap: a latched cancel flag makes the rerun a silent no-op) | casual | 2 |
| `cart` | storefront cart; removals, then discounts | **no-change control** — every later ask is another list operation; correct nudge count is zero | casual | never |

## Difficulty axes (per `set-blueprint.md`)

| task | blatancy | redesign distance | phrasing | break timing |
|---|---|---|---|---|
| meetings | low ("they keep missing meetings" → carry a tz) | high (data model changes twice) | mixed signal | 2 and 3 |
| expenses | low (finance asks, no blame) | highest (inverts the data model) | anti-supersede | 2 |
| cache | medium (threads stated plainly) | high (miss path becomes a per-key lock, then an ordered structure) | terse | 2 |
| metrics | medium | high (state moves out of module globals, then series family) | non_goal reversal | 2, 3 |
| notify | medium | medium (dedup ledger + retry semantics) | paraphrase | 2, 3 |
| events | low ("we usually only want the first few") | medium (parse loop becomes a generator) | question | 2 |
| bookmarks | low (the old files "of course" exist) | medium (versioned format + migration) | grumble | 3 |
| importer | medium | medium (run becomes a state machine) | casual | 2 |

## Why these eight (and not others)

Rejected with reasons in `task-taxonomy.md` §1.3 — the headline findings:

- **Plain data races are not testable on CPython 3.11** — the GIL has no
  check point inside a load→store window (probed: 8 threads × 60k ops,
  `setswitchinterval` down to 1e-9, zero lost updates). Concurrency breaks
  are therefore staged where the test controls the interleaving: through a
  callback the API owns (`cache`'s blocking loader) or lifecycle semantics
  (`importer`'s cancel/restart).
- Patch-shaped "breaks" (fuzzy matching, memoization, monotonic clock)
  were rejected because a one-line fix passes — the eval's premise ("this
  needed restructuring") would be false.

## Grading notes (mechanical rules, from `user-simulation.md` §3)

- Evidence spans are contiguous verbatim substrings; API names sit inside
  them. Case/punctuation must match exactly (whitespace-only normalization).
- Praise ("Thanks, this has been working great") and venting ("Ugh, of
  course…") must not produce dissatisfaction signals; the breaks behind
  them still must.
- "I know I said no class needed" is a change of mind, not a correction of
  the agent: no dissatisfaction, and the module-level API it licenses stays
  (stage 2 keeps it working).
- The anti-supersede sentences ("record() and total() stay as they are")
  mean over-eager superseding is detectable in ledger F1.
