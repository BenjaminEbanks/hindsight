# Evaluation

The paper's "Evaluation and Early Results" section tells the story of these experiments. Each
pre-registration was written before its runs, and results are added to the end of the file.

## Experiments

| Record | Question | Status |
|---|---|---|
| [`lab/PREREG.md`](lab/PREREG.md) | What should workers be aimed at? | Finished |
| [`lab/PREREG_reviewer.md`](lab/PREREG_reviewer.md) | Can a read-only reviewer catch real failures? | Finished: no, with this prompt |
| [`lab/PREREG_slopcode_pairs.md`](lab/PREREG_slopcode_pairs.md) | First comparison on SlopCodeBench | Stopped: not a result |
| [`lab/PREREG_four_setups.md`](lab/PREREG_four_setups.md) | Do designs improve the code, compared with no Hindsight? | Finished: no clear difference in ten tasks |

## Our own tasks

`tasks/` holds small project stories we wrote: a few casual messages, followed by hidden tests.
`tasks/v2/` and `tasks/v3/` are later sets. The paper explains why we moved on from them: they
weren't hard enough to tell the setups apart.

## Runners

All of these need `SAIL_API_KEY` and spend real credits, apart from `audit_contracts.py`.
The benchmark runners use Sail inference, so they don't read `.hindsight.toml`'s provider.

| Script | What it does |
|---|---|
| `scripts/evocode_pilot.py` | Runs benchmark tasks with or without Hindsight. Despite the name, this also runs SlopCodeBench, and it's the runner behind the paper's latest results. |
| `scripts/slopcode_prepare.py` | Converts SlopCodeBench tasks to the format that runner reads |
| `scripts/live_eval.py` | Runs our own tasks on this machine |
| `scripts/live_eval_swarm.py` | Runs our own tasks with one box per episode. **This one puts your real key in the box**, so use a key you can replace. |
| `scripts/analyze_swarm.py` | Summarises `live_eval_swarm.py` results |
| `scripts/audit_contracts.py` | Checks our tasks for bugs in their tests and instructions |
| `lab/proposal_lab.py`, `lab/reviewer_lab.py` | The two small lab experiments above |

The benchmarks aren't included, so pass their folder with `--data`. Get them from
[SlopCodeBench](https://arxiv.org/abs/2603.24755) and [EvoCode-Bench](https://arxiv.org/abs/2605.24110), and note that EvoCode-Bench
is CC BY-NC.
