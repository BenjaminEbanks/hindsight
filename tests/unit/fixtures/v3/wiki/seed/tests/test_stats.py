from wiki import stats
from wiki.notebook import Notebook


def build(tmp_path):
    nb = Notebook(str(tmp_path))
    nb.create("Home", "[[Guide]] and [[guide]] and [[Missing]]", ["start"])
    nb.create("Guide", "red red fox")
    nb.create("Other", "[[Guide]] [[Home]]")
    return nb


def test_counts(tmp_path):
    nb = build(tmp_path)
    assert stats.word_count(nb, "Guide") == 3
    assert stats.total_words(nb) == 10
    assert stats.vocabulary(nb) >= 2


def test_most_linked_counts_pages_not_mentions(tmp_path):
    nb = build(tmp_path)
    assert stats.most_linked(nb) == [("Guide", 2), ("Home", 1)]
    assert stats.most_linked(nb, 1) == [("Guide", 2)]


def test_tagless(tmp_path):
    assert stats.tagless(build(tmp_path)) == ["Guide", "Other"]
