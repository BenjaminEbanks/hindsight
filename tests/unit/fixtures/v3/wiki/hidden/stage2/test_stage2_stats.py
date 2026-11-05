from wiki import stats
from wiki.notebook import Notebook


def test_most_linked_follows_renames(tmp_path):
    nb = Notebook(str(tmp_path))
    nb.create("Guide")
    nb.create("A", "[[Guide]]")
    nb.create("B", "[[Guide]]")
    nb.rename("Guide", "Handbook")
    assert stats.most_linked(nb) == [("Handbook", 2)]
