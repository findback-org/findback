"""Regression for a real live Intake 'paperback' / inventory 'book' mismatch."""
import pytest

from findback.matching import coarse_candidates


@pytest.mark.parametrize("reported_category", [
    "book", "paperback", "paperback book", "hardback", "hardback book",
    "hardcover", "hardcover book", "novel", "textbook",
])
def test_book_descriptions_keep_the_inventory_book_as_a_coarse_candidate(reported_category):
    candidates = coarse_candidates(
        {"category": reported_category, "color": "red", "lost_place": "cafe"},
        [{"id": "I-004", "status": "AVAILABLE", "revision": 1,
          "coarse": {"category": "book", "color": "red", "found_place": "cafe window"},
          "private_facts": ["A private identifying detail"]}],
    )
    assert [candidate["item_id"] for candidate in candidates] == ["I-004"]
    assert "private_facts" not in candidates[0]
    assert "staff review" in candidates[0]["assessment"]


@pytest.mark.parametrize("reported_category", ["notebook", "ebook", "book bag", "bookcase"])
def test_other_categories_do_not_become_books_by_substring(reported_category):
    assert coarse_candidates(
        {"category": reported_category, "color": "red"},
        [{"id": "I-004", "status": "AVAILABLE", "revision": 1,
          "coarse": {"category": "book", "color": "red"}}],
    ) == []
