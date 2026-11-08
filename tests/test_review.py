"""The rule screen and hold decisions. All data is made up."""

import json
from pathlib import Path

import pytest

from whos_listening import review

VECTORS = json.loads((Path(__file__).parent / "vectors" / "rules.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("case", VECTORS, ids=lambda c: c["text"][:40])
def test_rule_vectors(case):
    assert review.rule_concern(case["text"]) == case["concern"]


def test_save_hold_reason_prefers_the_rule():
    assert review.save_hold_reason("Likes jazz", "") is None
    assert "outside content (read_file)" in review.save_hold_reason("Likes jazz", "read_file")
    assert review.save_hold_reason("Visit www.example.com", "read_file") == "It has a link or web address."


def test_forget_after_outside_content_waits():
    assert review.forget_hold_reason("") is None
    assert "forget waits" in review.forget_hold_reason("web_extract")


def test_outside_tools():
    assert review.is_outside_tool("read_file") and review.is_outside_tool("web_search")
    assert not review.is_outside_tool("wl_recall") and not review.is_outside_tool("todo")
    assert not review.is_outside_tool("") and not review.is_outside_tool("x", trusted=["x"])


def test_outside_book_is_bounded():
    book = review.OutsideBook(max_sessions=2)
    book.saw("a", "read_file")
    book.saw("a", "web_search")      # the first tool is kept
    assert book.get("a") == "read_file"
    book.saw("b", "x")
    book.saw("c", "y")
    assert book.get("a") == "" and book.get("c") == "y"
    book.saw("", "z")
    assert book.get("") == ""
