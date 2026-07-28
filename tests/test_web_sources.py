"""The e-avtomaktab scraper's parsing, exercised without touching the network."""

from __future__ import annotations

import pytest

from bot.sources.base import SourceError
from bot.sources.web_sources import EAvtomaktabSource, _extract_json_array

PAGE = """
<html><body>
<script>
  const other = [1, 2, 3];
  const questions = [{"QuestionId": 7, "TextLat": "Savol?", "TextRu": "Vopros?",
    "Image": "https://example.uz/a.jpg",
    "Answers": [{"TextLat": "Ha", "TextRu": "Da", "IsCorrect": false},
                {"TextLat": "Yo'q", "TextRu": "Net", "IsCorrect": true}]}];
</script>
</body></html>
"""


class TestExtraction:
    """Locating the embedded literal."""

    def test_finds_the_questions_array(self) -> None:
        payload = _extract_json_array(PAGE)
        assert len(payload) == 1
        assert payload[0]["QuestionId"] == 7

    def test_ignores_other_arrays(self) -> None:
        """Anchoring on the declaration keeps unrelated literals out."""
        assert _extract_json_array(PAGE)[0]["TextLat"] == "Savol?"

    def test_brackets_inside_strings_do_not_end_the_array(self) -> None:
        """Question text really does contain brackets; naive counting breaks."""
        page = """<script>const questions = [{"QuestionId": 1,
            "TextLat": "A ] bracket [ here", "Answers": []}];</script>"""
        payload = _extract_json_array(page)
        assert payload[0]["TextLat"] == "A ] bracket [ here"

    def test_missing_data_is_a_clear_error(self) -> None:
        """A site redesign should say so, not yield silent nonsense."""
        with pytest.raises(SourceError, match="layout has probably changed"):
            _extract_json_array("<html><body>nothing here</body></html>")

    def test_truncated_data_is_reported(self) -> None:
        with pytest.raises(SourceError, match="truncated"):
            _extract_json_array('<script>const questions = [{"a": 1}</script>')


class TestRecordMapping:
    """Turning a site entry into an importable record."""

    def test_uses_the_requested_language(self) -> None:
        entry = _extract_json_array(PAGE)[0]

        uz = EAvtomaktabSource(language="uz")._to_record(entry)
        ru = EAvtomaktabSource(language="ru")._to_record(entry)

        assert uz is not None and uz["text"] == "Savol?"
        assert ru is not None and ru["text"] == "Vopros?"

    def test_correct_index_follows_is_correct(self) -> None:
        record = EAvtomaktabSource(language="uz")._to_record(_extract_json_array(PAGE)[0])
        assert record is not None
        assert record["correct_index"] == 1
        assert record["options"][record["correct_index"]] == "Yo'q"

    def test_image_url_is_carried_through(self) -> None:
        record = EAvtomaktabSource(language="uz")._to_record(_extract_json_array(PAGE)[0])
        assert record is not None
        assert record["image_url"] == "https://example.uz/a.jpg"

    def test_entry_without_a_correct_answer_is_dropped(self) -> None:
        """No correct option means no quiz poll; better skipped than half-sent."""
        entry = {
            "QuestionId": 9,
            "TextLat": "Savol?",
            "Answers": [
                {"TextLat": "A", "IsCorrect": False},
                {"TextLat": "B", "IsCorrect": False},
            ],
        }
        assert EAvtomaktabSource(language="uz")._to_record(entry) is None

    def test_entry_with_one_option_is_dropped(self) -> None:
        entry = {
            "QuestionId": 9,
            "TextLat": "Savol?",
            "Answers": [{"TextLat": "A", "IsCorrect": True}],
        }
        assert EAvtomaktabSource(language="uz")._to_record(entry) is None

    def test_first_correct_answer_wins(self) -> None:
        """The bank occasionally flags two; a poll accepts exactly one."""
        entry = {
            "QuestionId": 9,
            "TextLat": "Savol?",
            "Answers": [
                {"TextLat": "A", "IsCorrect": False},
                {"TextLat": "B", "IsCorrect": True},
                {"TextLat": "C", "IsCorrect": True},
            ],
        }
        record = EAvtomaktabSource(language="uz")._to_record(entry)
        assert record is not None
        assert record["correct_index"] == 1

    def test_unsupported_language_is_rejected_at_construction(self) -> None:
        with pytest.raises(SourceError, match="Unsupported language"):
            EAvtomaktabSource(language="de")
