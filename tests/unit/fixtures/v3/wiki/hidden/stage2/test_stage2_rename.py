import pytest

from wiki import export, graph
from wiki.notebook import Notebook, PageExists, PageNotFound

def legacy_file(title, body):
    return f"title: {title}\ntags: \n\n{body}"


def renamed(tmp_path):
    nb = Notebook(str(tmp_path / "nb"))
    nb.create("Old Name", "content", ["keep"])
    nb.create("Linker", "see [[Old Name]]")
    nb.rename("Old Name", "New Name")
    return nb


def test_page_moves_and_old_title_is_gone(tmp_path):
    nb = renamed(tmp_path)
    assert nb.get("New Name").body == "content"
    assert nb.get("New Name").tags == ["keep"]
    assert nb.titles() == ["Linker", "New Name"]
    with pytest.raises(PageNotFound):
        nb.get("Old Name")


def test_links_follow_but_other_pages_text_is_untouched(tmp_path):
    nb = renamed(tmp_path)
    assert nb.get("Linker").body == "see [[Old Name]]"
    assert nb.target_of("Linker", "Old Name") == "New Name"
    assert graph.backlinks(nb, "New Name") == ["Linker"]
    assert graph.backlinks(nb, "Old Name") == []
    assert graph.broken_links(nb) == {}
    assert "New Name" not in graph.orphans(nb)


def test_search_uses_the_new_title(tmp_path):
    nb = renamed(tmp_path)
    assert nb.search("new") == ["New Name"]
    assert nb.search("name") == ["Linker", "New Name"]
    assert nb.search("old") == ["Linker"]  # its body still says Old Name
    nb.delete("Linker")
    assert nb.search("old") == []


def test_export_links_to_the_new_file_keeping_the_written_text(tmp_path):
    nb = renamed(tmp_path)
    out = tmp_path / "site"
    export.export_html(nb, str(out))
    assert '<a href="new-name.html">Old Name</a>' in (out / "linker.html").read_text()
    assert (out / "new-name.html").exists()
    assert not (out / "old-name.html").exists()


def test_rename_onto_a_taken_title_is_refused(tmp_path):
    nb = renamed(tmp_path)
    with pytest.raises(PageExists):
        nb.rename("Linker", "new name")
    assert nb.titles() == ["Linker", "New Name"]


def test_chain_of_renames_and_links_written_with_old_titles(tmp_path):
    nb = renamed(tmp_path)
    nb.rename("New Name", "Newest")
    assert nb.target_of("Linker", "Old Name") == "Newest"
    nb.create("Later", "[[New Name]] and [[Old Name]]")
    assert nb.target_of("Later", "New Name") == "Newest"
    assert nb.target_of("Later", "Old Name") == "Newest"


def test_rename_survives_reopening(tmp_path):
    renamed(tmp_path)
    nb = Notebook(str(tmp_path / "nb"))
    assert nb.target_of("Linker", "Old Name") == "New Name"
    assert (tmp_path / "nb" / "pages" / "new-name.md").exists()
    assert not (tmp_path / "nb" / "pages" / "old-name.md").exists()


def test_existing_folder_and_hand_dropped_files_still_work(tmp_path):
    pages = tmp_path / "nb" / "pages"
    pages.mkdir(parents=True)
    (pages / "guide.md").write_text(legacy_file("Guide", "hello"))
    (pages / "home.md").write_text(legacy_file("Home", "see [[Guide]]"))
    nb = Notebook(str(tmp_path / "nb"))
    nb.rename("Guide", "Handbook")
    (pages / "notes.md").write_text(legacy_file("Notes", "use [[Handbook]]"))
    nb = Notebook(str(tmp_path / "nb"))
    assert nb.get("Notes").body == "use [[Handbook]]"
    assert nb.target_of("Notes", "Handbook") == "Handbook"
    assert nb.target_of("Home", "Guide") == "Handbook"
    assert graph.backlinks(nb, "Handbook") == ["Home", "Notes"]
