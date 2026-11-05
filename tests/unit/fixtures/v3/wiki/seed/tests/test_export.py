from wiki import export
from wiki.notebook import Notebook


def test_export(tmp_path):
    nb = Notebook(str(tmp_path / "nb"))
    nb.create("Home", "[[Guide Book]] and [[Missing]]")
    nb.create("Guide Book")
    out = tmp_path / "site"
    assert export.export_html(nb, str(out)) == ["guide-book.html", "home.html"]
    page = (out / "home.html").read_text()
    assert '<a href="guide-book.html">Guide Book</a>' in page
    assert '<span class="broken">Missing</span>' in page
    assert "guide-book.html" in (out / "index.html").read_text()
