# Pre-registration: do Hindsight's nudges improve the code on SlopCodeBench?

Written before any of these runs. Date: 2026-11-03.

## Setup
- Tasks: the 32 SlopCodeBench problems not used to tune the review prompt. Excluded
  for tuning: `trajectory_api`, `mvvault`, `textdrop`, `execution_server`.
- A task whose reference solution fails its own tests is dropped before any model
  run, and the drop is reported.
- Two copies of each task, started together: the agent alone, and the agent with
  Hindsight. Same model in every role and both copies: `zai-org/GLM-5.3-Flash`.
- One run per task per copy. The simulated user agrees to what the agent recommends.
- Code frozen at the commit that adds this file. A fix made during the runs is
  recorded, and affected runs are rerun or dropped from the result.

## What is compared
Paired by task, Hindsight minus alone, on checkpoints scored in both copies:
1. Erosion and verbosity at the last checkpoint scored in both (lower is better).
2. Isolated, strict and regression pass rates, averaged over those checkpoints.

Reported with the median difference, the win/loss/tie count and a sign test. There are
several outcomes, so one small p-value alone is not a finding.

## How it is read
- Every task with both copies scored counts, including tasks where Hindsight never
  delivered. That is the fair estimate of turning Hindsight on.
- Tasks where a design was delivered or used are also reported, as exploratory only:
  which tasks fire isn't random, so this can't show the nudges caused a difference.
- Pairs with no delivery show how much two runs differ by chance.
- A task with an infrastructure failure in either copy is dropped and listed.
- Scores can't be compared with the published leaderboard: this runner keeps the
  conversation between checkpoints, and the published runner appears to reset it.

## Also reported
Cost per task in each copy, runs started, designs delivered, how far behind they were,
and what the agent did with them.

## Stop rule
Stop launching work and ask the user if total spend nears $150.
