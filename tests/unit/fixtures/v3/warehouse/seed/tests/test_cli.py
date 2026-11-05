import io

from stockroom.cli import main


def run(path, *args):
    out = io.StringIO()
    code = main(["--file", str(path), *args], out)
    return code, out.getvalue()


def test_day_in_the_life(tmp_path):
    f = tmp_path / "stock.json"
    run(f, "add-item", "A", "Widget")
    run(f, "receive", "A", "5")
    code, out = run(f, "place", "acme", "A=2")
    assert code == 0 and out.strip() == "1"
    _, status = run(f, "status")
    assert "A on_hand=5 available=3" in status
    run(f, "ship", "1")
    _, status = run(f, "status")
    assert "A on_hand=3 available=3" in status
    assert run(f, "audit")[0] == 0
