"""Evaluation harness.

Staged coding tasks: each stage reveals requirements that break the earlier
design, with hidden acceptance tests. Compared against two baselines: the main agent alone,
and the main agent asked to redesign at the end of the session.
"""

from hindsight.eval.tasks import Stage, Task  # noqa: F401
