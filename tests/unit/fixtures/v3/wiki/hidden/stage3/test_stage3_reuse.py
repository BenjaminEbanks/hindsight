from wiki import export, graph
from wiki.notebook import Notebook


def reused(tmp_path):
    nb = Notebook(str(tmp_path / "nb"))
    nb.create("Alpha", "the original")
    nb.create("Before", "[[Alpha]]")
    nb.rename("Alpha", "Beta")
    nb.create("Alpha", "the newcomer")
    nb.create("After", "[[Alpha]]")
    return nb


def test_links_keep_the_page_they_were_written_for(tmp_path):
    nb = reused(tmp_path)
    assert nb.target_of("Before", "Alpha") == "Beta"
    assert nb.target_of("After", "Alpha") == "Alpha"
    assert graph.backlinks(nb, "Beta") == ["Before"]
    assert graph.backlinks(nb, "Alpha") == ["After"]


def test_export_follows_the_same_targets(tmp_path):
    nb = reused(tmp_path)
    out = tmp_path / "site"
    export.export_html(nb, str(out))
    assert 'href="beta.html"' in (out / "before.html").read_text()
    assert 'href="alpha.html"' in (out / "after.html").read_text()


def test_targets_survive_reopening(tmp_path):
    reused(tmp_path)
    nb = Notebook(str(tmp_path / "nb"))
    assert nb.target_of("Before", "Alpha") == "Beta"
    assert nb.target_of("After", "Alpha") == "Alpha"
    assert nb.get("Alpha").body == "the newcomer"


def test_editing_keeps_a_link_dropping_it_forgets_it(tmp_path):
    nb = reused(tmp_path)
    nb.update("Before", "[[Alpha]] plus more words")
    assert nb.target_of("Before", "Alpha") == "Beta"
    nb.update("Before", "no links now")
    nb.update("Before", "[[Alpha]] written fresh")
    assert nb.target_of("Before", "Alpha") == "Alpha"


def test_unresolved_link_binds_when_the_page_appears(tmp_path):
    nb = Notebook(str(tmp_path / "nb"))
    nb.create("Waiting", "[[Soon]]")
    assert nb.target_of("Waiting", "Soon") is None
    nb.create("Soon")
    nb.rename("Soon", "Later")
    nb.create("Soon")
    assert nb.target_of("Waiting", "Soon") == "Later"


def test_old_title_means_the_most_recent_holder(tmp_path):
    nb = Notebook(str(tmp_path / "nb"))
    nb.create("A")
    nb.rename("A", "First")
    nb.create("A")
    nb.rename("A", "Second")
    nb.create("Fresh", "[[A]]")
    assert nb.target_of("Fresh", "A") == "Second"


def test_deleting_a_target_breaks_links_until_the_title_returns(tmp_path):
    nb = Notebook(str(tmp_path / "nb"))
    nb.create("A")
    nb.create("L", "[[A]]")
    nb.rename("A", "B")
    nb.delete("B")
    assert nb.target_of("L", "A") is None
    assert graph.broken_links(nb) == {"L": ["A"]}
    nb.create("Fresh", "[[B]] [[A]]")
    assert graph.broken_links(nb)["Fresh"] == ["B", "A"]
    nb.create("A")
    assert nb.target_of("L", "A") == "A"
    assert nb.target_of("Fresh", "A") == "A"


def test_links_in_files_from_the_old_folder_bind_on_first_open(tmp_path):
    pages = tmp_path / "nb" / "pages"
    pages.mkdir(parents=True)
    (pages / "a.md").write_text("title: A\ntags: \n\nhi")
    (pages / "l.md").write_text("title: L\ntags: \n\n[[A]]")
    nb = Notebook(str(tmp_path / "nb"))
    nb.rename("A", "B")
    nb.create("A")
    nb = Notebook(str(tmp_path / "nb"))
    assert nb.target_of("L", "A") == "B"
