from wiki import storage
from wiki.models import Page, slugify


def test_slugify():
    assert slugify("Home Page!") == "home-page"


def test_round_trip_keeps_blank_lines_in_body():
    page = Page("T", "one\n\ntwo\n", ["a", "b"])
    assert storage.parse_page(storage.render_page(page)) == page


def test_format_is_plain_header_then_body():
    assert storage.render_page(Page("T", "hi", ["a"])) == "title: T\ntags: a\n\nhi"
