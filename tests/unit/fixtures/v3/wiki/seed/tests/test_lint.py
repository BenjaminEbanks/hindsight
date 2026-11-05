from wiki import lint
from wiki.notebook import Notebook


def test_findings(tmp_path):
    nb = Notebook(str(tmp_path))
    nb.create("Clean", "fine", ["a"])
    nb.create("Empty", "", ["a"])
    nb.create("Bare", "text")
    nb.create("Long", "x" * 130, ["a"])
    nb.create("lower", "text", ["a"])
    found = lint.lint(nb)
    assert lint.Finding("Empty", "empty") in found
    assert lint.Finding("Bare", "untagged") in found
    assert lint.Finding("Long", "long-line", "line 1") in found
    assert lint.Finding("lower", "title-style") in found
    assert not [f for f in found if f.title == "Clean"]
    assert lint.summary(found)["empty"] == 1
