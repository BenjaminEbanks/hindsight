import io

from wiki.cli import main


def run(root, *args):
    out = io.StringIO()
    code = main(["--root", str(root), *args], out)
    return code, out.getvalue()


def test_day_to_day(tmp_path):
    run(tmp_path, "new", "Home", "see [[Guide]]")
    assert run(tmp_path, "check") == (1, "Home: Guide\n")
    run(tmp_path, "new", "Guide", "short")
    assert run(tmp_path, "check")[0] == 0
    assert run(tmp_path, "search", "short")[1] == "Guide\n"
    assert run(tmp_path, "show", "Guide")[1] == "short\n"
    assert run(tmp_path, "export", str(tmp_path / "site"))[0] == 0
    assert (tmp_path / "site" / "guide.html").exists()
