import pytest

from wiki import backup
from wiki.notebook import Notebook


def test_round_trip_through_a_file(tmp_path):
    nb = Notebook(str(tmp_path / "a"))
    nb.create("Home", "see [[Guide]]", ["start"])
    nb.create("Guide", "text")
    path = str(tmp_path / "b.json")
    backup.save_backup(nb, path)
    copy = backup.restore(str(tmp_path / "copy"), backup.load_backup(path))
    assert copy.titles() == ["Guide", "Home"]
    assert copy.get("Home").tags == ["start"]
    assert copy.target_of("Home", "Guide") == "Guide"


def test_restore_refuses_a_folder_with_pages(tmp_path):
    nb = Notebook(str(tmp_path))
    nb.create("Home")
    with pytest.raises(ValueError):
        backup.restore(str(tmp_path), backup.dump(nb))
