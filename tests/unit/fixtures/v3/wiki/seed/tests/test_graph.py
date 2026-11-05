from wiki import graph
from wiki.notebook import Notebook


def build(tmp_path):
    nb = Notebook(str(tmp_path))
    nb.create("Home", "[[Guide]] and [[Missing]] and [[Home]]")
    nb.create("Guide", "back to [[Home]]")
    nb.create("Lonely")
    return nb


def test_backlinks(tmp_path):
    nb = build(tmp_path)
    assert graph.backlinks(nb, "Home") == ["Guide", "Home"]


def test_broken(tmp_path):
    assert graph.broken_links(build(tmp_path)) == {"Home": ["Missing"]}


def test_orphans(tmp_path):
    assert graph.orphans(build(tmp_path)) == ["Lonely"]
