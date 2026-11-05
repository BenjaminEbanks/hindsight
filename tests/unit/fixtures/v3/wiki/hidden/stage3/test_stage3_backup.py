import json

from wiki import backup, stats
from wiki.notebook import Notebook


def reused(tmp_path):
    nb = Notebook(str(tmp_path / "nb"))
    nb.create("Alpha", "the original")
    nb.create("Before", "[[Alpha]]")
    nb.rename("Alpha", "Beta")
    nb.create("Alpha", "the newcomer")
    nb.create("After", "[[Alpha]]")
    nb.create("Also", "[[Alpha]]")
    return nb


def test_most_linked_follows_attachments(tmp_path):
    assert stats.most_linked(reused(tmp_path)) == [("Alpha", 2), ("Beta", 1)]


def test_backup_carries_attachments_and_stays_json(tmp_path):
    data = json.loads(json.dumps(backup.dump(reused(tmp_path))))
    copy = backup.restore(str(tmp_path / "copy"), data)
    assert copy.target_of("Before", "Alpha") == "Beta"
    assert copy.target_of("After", "Alpha") == "Alpha"
    assert copy.get("Alpha").body == "the newcomer"


def test_backups_from_cold_storage_still_restore(tmp_path):
    data = {"pages": [{"title": "A", "body": "[[B]]", "tags": ["x"]},
                      {"title": "B", "body": "hi", "tags": []}]}
    copy = backup.restore(str(tmp_path / "copy"), data)
    assert copy.target_of("A", "B") == "B"
    assert copy.get("A").tags == ["x"]
    copy.rename("B", "C")
    copy.create("B")
    assert copy.target_of("A", "B") == "C"
