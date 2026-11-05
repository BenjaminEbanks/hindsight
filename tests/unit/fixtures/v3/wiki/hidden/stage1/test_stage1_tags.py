from wiki import graph
from wiki.notebook import Notebook


def test_tags_ignore_case(tmp_path):
    nb = Notebook(str(tmp_path))
    nb.create("B Page", tags=["Python", "ops"])
    nb.create("A Page", tags=["python"])
    nb.create("C Page")
    assert nb.pages_with_tag("PYTHON") == ["A Page", "B Page"]
    assert nb.pages_with_tag("nope") == []
    assert graph.tag_counts(nb) == {"python": 2, "ops": 1}
