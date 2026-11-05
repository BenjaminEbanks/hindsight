import pytest

from wiki.notebook import Notebook, PageNotFound


def test_every_change_is_a_revision_and_history_crosses_renames(tmp_path):
    nb = Notebook(str(tmp_path))
    nb.create("A", "one")
    nb.update("A", "two")
    nb.rename("A", "B")
    assert nb.history("B") == [
        {"rev": 1, "title": "A", "body": "one"},
        {"rev": 2, "title": "A", "body": "two"},
        {"rev": 3, "title": "B", "body": "two"},
    ]
    with pytest.raises(PageNotFound):
        nb.history("A")
    assert Notebook(str(tmp_path)).history("B")[0]["body"] == "one"


def test_a_reused_title_starts_its_own_history(tmp_path):
    nb = Notebook(str(tmp_path))
    nb.create("A", "first")
    nb.rename("A", "B")
    nb.create("A", "second")
    assert [r["body"] for r in nb.history("A")] == ["second"]
    assert len(nb.history("B")) == 2


def test_revert_restores_the_body_as_a_new_revision_keeping_the_title(tmp_path):
    nb = Notebook(str(tmp_path))
    nb.create("A", "red fox")
    nb.update("A", "blue fox")
    nb.rename("A", "B")
    nb.revert("B", 1)
    assert nb.get("B").body == "red fox"
    assert nb.titles() == ["B"]
    assert nb.history("B")[-1] == {"rev": 4, "title": "B", "body": "red fox"}
    assert nb.search("red") == ["B"]
    assert nb.search("blue") == []
    assert Notebook(str(tmp_path)).get("B").body == "red fox"
    with pytest.raises(ValueError):
        nb.revert("B", 9)


def test_revert_restores_where_the_links_pointed_back_then(tmp_path):
    nb = Notebook(str(tmp_path))
    nb.create("Alpha", "the original")
    nb.create("L", "[[Alpha]]")
    nb.rename("Alpha", "Beta")
    nb.create("Alpha", "the newcomer")
    nb.update("L", "no links")
    nb.update("L", "[[Alpha]] fresh")
    assert nb.target_of("L", "Alpha") == "Alpha"
    nb.revert("L", 1)
    assert nb.target_of("L", "Alpha") == "Beta"
    nb.revert("L", 3)
    assert nb.target_of("L", "Alpha") == "Alpha"
    assert Notebook(str(tmp_path)).target_of("L", "Alpha") == "Alpha"


def test_revert_when_the_old_target_is_gone_binds_like_a_new_link(tmp_path):
    nb = Notebook(str(tmp_path))
    nb.create("Alpha")
    nb.create("L", "[[Alpha]]")
    nb.update("L", "no links")
    nb.delete("Alpha")
    nb.revert("L", 1)
    assert nb.target_of("L", "Alpha") is None
    nb.create("Alpha")
    assert nb.target_of("L", "Alpha") == "Alpha"


def test_pages_from_the_old_folder_start_at_revision_one(tmp_path):
    pages = tmp_path / "pages"
    pages.mkdir()
    (pages / "a.md").write_text("title: A\ntags: \n\nhi")
    nb = Notebook(str(tmp_path))
    assert nb.history("A") == [{"rev": 1, "title": "A", "body": "hi"}]
    nb.update("A", "there")
    assert Notebook(str(tmp_path)).history("A")[-1]["rev"] == 2
