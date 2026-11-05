import json

from wiki import backup
from wiki.notebook import Notebook


def test_backup_carries_history_and_revert_still_restores_links(tmp_path):
    nb = Notebook(str(tmp_path / "nb"))
    nb.create("Alpha")
    nb.create("L", "[[Alpha]]")
    nb.rename("Alpha", "Beta")
    nb.create("Alpha")
    nb.update("L", "no links")
    data = json.loads(json.dumps(backup.dump(nb)))
    copy = backup.restore(str(tmp_path / "copy"), data)
    assert [r["body"] for r in copy.history("L")] == ["[[Alpha]]", "no links"]
    copy.revert("L", 1)
    assert copy.target_of("L", "Alpha") == "Beta"
