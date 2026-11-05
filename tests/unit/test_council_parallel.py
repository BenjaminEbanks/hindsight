"""Critics run together, and their order and failures stay as they were."""
import threading
import time

from hindsight.council import REVIEW_LENSES, Council


class _Cfg:
    class council:
        reviewers = 3


class _Store:
    def __init__(self):
        self.saved = []

    def save_critique(self, run_id, lens, labels, ranking, raw, model=""):
        self.saved.append(lens)


def _designs():
    return [{"id": "p1", "patch": "", "summary": "a"}, {"id": "p2", "patch": "", "summary": "b"}]


def test_critics_run_at_the_same_time_and_keep_lens_order():
    barrier = threading.Barrier(3, timeout=5)  # breaks unless all three are in at once

    def model(prompt):
        barrier.wait()
        time.sleep(0.01 if REVIEW_LENSES[0][0] in prompt else 0)
        return "FINAL RANKING:\n1. Proposal A\n2. Proposal B"

    store = _Store()
    critiques, _ = Council(_Cfg, store, model).critique_round("r", "msgs", _designs())
    names = [lens for lens, _ in REVIEW_LENSES[:3]]
    assert [c["reviewer"] for c in critiques] == names
    assert store.saved == names


def test_one_failed_critic_does_not_stop_the_others():
    def model(prompt):
        if REVIEW_LENSES[1][0] in prompt:
            raise RuntimeError("down")
        return "FINAL RANKING:\n1. Proposal A\n2. Proposal B"

    critiques, _ = Council(_Cfg, _Store(), model).critique_round("r", "msgs", _designs())
    assert [c["reviewer"] for c in critiques] == [REVIEW_LENSES[0][0], REVIEW_LENSES[2][0]]
