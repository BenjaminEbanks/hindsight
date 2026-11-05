import os

import pytest

from wiki.notebook import Notebook, PageExists, PageNotFound

SAMPLE = os.path.join(os.path.dirname(__file__), "..", "data", "sample")


def test_create_get_reopen(tmp_path):
    nb = Notebook(str(tmp_path))
    nb.create("Home Page", "hello", ["start"])
    again = Notebook(str(tmp_path))
    assert again.get("home page").body == "hello"
    assert again.get("Home Page").tags == ["start"]


def test_duplicate_refused(tmp_path):
    nb = Notebook(str(tmp_path))
    nb.create("Home")
    with pytest.raises(PageExists):
        nb.create("HOME")


def test_update_and_delete(tmp_path):
    nb = Notebook(str(tmp_path))
    nb.create("Home", "one")
    nb.update("Home", "two")
    assert Notebook(str(tmp_path)).get("Home").body == "two"
    nb.delete("Home")
    with pytest.raises(PageNotFound):
        nb.get("Home")
    assert Notebook(str(tmp_path)).titles() == []


def test_search_tracks_edits(tmp_path):
    nb = Notebook(str(tmp_path))
    nb.create("Home", "red fox")
    assert nb.search("fox red") == ["Home"]
    nb.update("Home", "blue fox")
    assert nb.search("red") == []
    assert nb.search("home") == ["Home"]


def test_target_of(tmp_path):
    nb = Notebook(str(tmp_path))
    nb.create("Home", "see [[about us]] and [[Nope]]")
    nb.create("About Us")
    assert nb.target_of("Home", "about us") == "About Us"
    assert nb.target_of("Home", "Nope") is None


def test_sample_notebook_loads():
    assert Notebook(SAMPLE).titles() == ["Home", "Onboarding", "Style Guide"]
