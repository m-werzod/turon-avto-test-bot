"""Question parsing and the file readers."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bot.sources.base import QuestionValidationError, SourceError, parse_record
from bot.sources.file_sources import CsvQuestionSource, JsonQuestionSource
from bot.sources.registry import build_source, discover_data_files

OPTIONS = ["Ha", "Yo'q", "Faqat kunduzi", "Faqat kechasi"]


def base_record(**overrides: object) -> dict[str, object]:
    """A minimal valid record, with optional overrides."""
    record: dict[str, object] = {
        "id": "1",
        "question": "Svetofor qizil rangda yonsa nima qilish kerak?",
        "options": OPTIONS,
        "correct": 1,
    }
    record.update(overrides)
    return record


class TestCorrectAnswerResolution:
    """The three answer spellings must never be confused for one another."""

    def test_zero_based_index(self) -> None:
        question = parse_record(base_record(correct_index=2, correct=None), location="t")
        assert question.correct_index == 2

    def test_one_based_number(self) -> None:
        question = parse_record(base_record(correct=3), location="t")
        assert question.correct_index == 2

    def test_letter(self) -> None:
        question = parse_record(base_record(correct="C"), location="t")
        assert question.correct_index == 2

    def test_answer_text(self) -> None:
        question = parse_record(
            base_record(correct=None, correct_answer="Faqat kunduzi"), location="t"
        )
        assert question.correct_index == 2

    def test_answer_text_ignores_case_and_spacing(self) -> None:
        question = parse_record(
            base_record(correct=None, correct_answer="  faqat  KUNDUZI "), location="t"
        )
        assert question.correct_index == 2

    def test_zero_is_rejected_as_ambiguous(self) -> None:
        """``correct: 0`` could mean either convention, so it is refused.

        Guessing here would silently mark the wrong answer correct.
        """
        with pytest.raises(QuestionValidationError, match="1-based"):
            parse_record(base_record(correct=0), location="t")

    def test_out_of_range_index(self) -> None:
        with pytest.raises(QuestionValidationError, match="out of range"):
            parse_record(base_record(correct_index=7, correct=None), location="t")

    def test_unmatched_answer_text(self) -> None:
        with pytest.raises(QuestionValidationError, match="does not match"):
            parse_record(base_record(correct=None, correct_answer="Nothing"), location="t")

    def test_missing_answer(self) -> None:
        with pytest.raises(QuestionValidationError, match="no correct answer"):
            parse_record(base_record(correct=None), location="t")


class TestOptionLayouts:
    """Option columns come in several shapes across real exports."""

    def test_numbered_columns(self) -> None:
        record = {
            "id": "1",
            "question": "Q?",
            "option1": OPTIONS[0],
            "option2": OPTIONS[1],
            "option3": OPTIONS[2],
            "option4": OPTIONS[3],
            "correct": 2,
        }
        assert parse_record(record, location="t").correct_index == 1

    def test_lettered_columns(self) -> None:
        record = {"id": "1", "text": "Q?", "correct": 4}
        record.update(dict(zip("abcd", OPTIONS, strict=True)))
        assert parse_record(record, location="t").correct_index == 3

    def test_pipe_delimited_single_cell(self) -> None:
        question = parse_record(base_record(options="|".join(OPTIONS)), location="t")
        assert question.options == OPTIONS

    def test_uppercase_headers(self) -> None:
        record = {"ID": "1", "QUESTION": "Q?", "OPTIONS": OPTIONS, "CORRECT": 1}
        assert parse_record(record, location="t").correct_index == 0

    def test_uzbek_field_names(self) -> None:
        record = {"id": "1", "savol": "Q?", "javoblar": OPTIONS, "togri_javob": 2}
        assert parse_record(record, location="t").correct_index == 1

    def test_wrong_option_count_rejected(self) -> None:
        with pytest.raises(QuestionValidationError, match="exactly 4 options"):
            parse_record(base_record(options=OPTIONS[:3]), location="t")

    def test_duplicate_options_rejected(self) -> None:
        with pytest.raises(QuestionValidationError, match="duplicates"):
            parse_record(base_record(options=["a", "a", "b", "c"]), location="t")


class TestClamping:
    """Text must arrive already within the Bot API limits."""

    def test_question_truncated_to_poll_limit(self) -> None:
        question = parse_record(base_record(question="word " * 200), location="t")
        assert len(question.text) <= 300

    def test_option_truncated(self) -> None:
        long_options = ["x" * 300, *OPTIONS[1:]]
        question = parse_record(base_record(options=long_options), location="t")
        assert all(len(option) <= 100 for option in question.options)

    def test_explanation_truncated(self) -> None:
        question = parse_record(base_record(explanation="e" * 500), location="t")
        assert question.explanation is not None
        assert len(question.explanation) <= 200

    def test_newlines_collapsed(self) -> None:
        question = parse_record(base_record(question="Line one\n\nLine two"), location="t")
        assert "\n" not in question.text
        assert question.text == "Line one Line two"


class TestJsonSource:
    """Reading .json files."""

    async def _collect(self, source: JsonQuestionSource) -> list[object]:
        return [question async for question in source.fetch()]

    async def test_bare_array(self, tmp_path: Path) -> None:
        path = tmp_path / "q.json"
        path.write_text(json.dumps([base_record(id=str(i)) for i in range(3)]), encoding="utf-8")
        questions = await self._collect(JsonQuestionSource(path))
        assert len(questions) == 3

    async def test_wrapped_object_with_defaults(self, tmp_path: Path) -> None:
        path = tmp_path / "q.json"
        path.write_text(
            json.dumps(
                {
                    "source": "official",
                    "language": "ru",
                    "questions": [base_record(id="1")],
                }
            ),
            encoding="utf-8",
        )
        source = JsonQuestionSource(path)
        questions = await self._collect(source)
        assert source.name == "json:official"
        assert questions[0].language == "ru"  # type: ignore[attr-defined]

    async def test_invalid_records_are_skipped_not_fatal(self, tmp_path: Path) -> None:
        """One bad row out of many must not cost the whole import."""
        path = tmp_path / "q.json"
        path.write_text(
            json.dumps(
                [
                    base_record(id="1"),
                    {"id": "2", "question": "broken"},  # no options, no answer
                    base_record(id="3"),
                ]
            ),
            encoding="utf-8",
        )
        source = JsonQuestionSource(path)
        questions = await self._collect(source)
        assert len(questions) == 2
        assert len(source.errors) == 1

    async def test_strict_mode_aborts(self, tmp_path: Path) -> None:
        path = tmp_path / "q.json"
        path.write_text(json.dumps([{"id": "1", "question": "broken"}]), encoding="utf-8")
        source = JsonQuestionSource(path, strict=True)
        with pytest.raises(QuestionValidationError):
            await self._collect(source)

    async def test_malformed_json_reports_position(self, tmp_path: Path) -> None:
        path = tmp_path / "q.json"
        path.write_text('{"questions": [', encoding="utf-8")
        with pytest.raises(SourceError, match="not valid JSON"):
            await self._collect(JsonQuestionSource(path))

    async def test_missing_file(self, tmp_path: Path) -> None:
        with pytest.raises(SourceError, match="not found"):
            await self._collect(JsonQuestionSource(tmp_path / "absent.json"))

    async def test_empty_file(self, tmp_path: Path) -> None:
        path = tmp_path / "q.json"
        path.write_text("", encoding="utf-8")
        with pytest.raises(SourceError, match="empty"):
            await self._collect(JsonQuestionSource(path))


class TestCsvSource:
    """Reading .csv files."""

    async def test_comma_delimited(self, tmp_path: Path) -> None:
        path = tmp_path / "q.csv"
        path.write_text(
            "id,question,option1,option2,option3,option4,correct\n"
            "1,Question one?,A,B,C,D,2\n"
            "2,Question two?,E,F,G,H,1\n",
            encoding="utf-8",
        )
        source = CsvQuestionSource(path)
        questions = [question async for question in source.fetch()]
        assert len(questions) == 2
        assert questions[0].correct_index == 1

    async def test_semicolon_delimited(self, tmp_path: Path) -> None:
        """Excel in a ru/uz locale writes semicolons; those files must work."""
        path = tmp_path / "q.csv"
        path.write_text(
            "id;question;option1;option2;option3;option4;correct\n"
            "1;Question one?;A;B;C;D;3\n"
            "2;Question two?;E;F;G;H;1\n",
            encoding="utf-8",
        )
        source = CsvQuestionSource(path)
        questions = [question async for question in source.fetch()]
        assert len(questions) == 2
        assert questions[0].correct_index == 2

    async def test_header_only(self, tmp_path: Path) -> None:
        path = tmp_path / "q.csv"
        path.write_text("id,question,option1,option2,option3,option4,correct\n", encoding="utf-8")
        with pytest.raises(SourceError, match="no data rows"):
            [question async for question in CsvQuestionSource(path).fetch()]

    async def test_row_number_is_a_stable_fallback_id(self, tmp_path: Path) -> None:
        """A file with no id column still imports idempotently."""
        path = tmp_path / "q.csv"
        path.write_text(
            "question,option1,option2,option3,option4,correct\nQ one?,A,B,C,D,1\n",
            encoding="utf-8",
        )
        questions = [question async for question in CsvQuestionSource(path).fetch()]
        assert questions[0].external_id == "1"


class TestRegistry:
    """Format dispatch and file discovery."""

    def test_builds_the_right_reader(self, tmp_path: Path) -> None:
        for name, expected in (
            ("a.json", "json"),
            ("b.csv", "csv"),
            ("c.xlsx", "xlsx"),
        ):
            path = tmp_path / name
            path.touch()
            assert build_source(path).format_name == expected  # type: ignore[attr-defined]

    def test_rejects_unsupported_extension(self, tmp_path: Path) -> None:
        with pytest.raises(SourceError, match="Unsupported file type"):
            build_source(tmp_path / "questions.pdf")

    def test_discovery_ignores_unrelated_files(self, tmp_path: Path) -> None:
        (tmp_path / "questions.json").touch()
        (tmp_path / "notes.txt").touch()
        (tmp_path / "sheet.xlsx").touch()
        found = {path.name for path in discover_data_files(tmp_path)}
        assert found == {"questions.json", "sheet.xlsx"}

    def test_discovery_on_missing_directory(self, tmp_path: Path) -> None:
        assert discover_data_files(tmp_path / "nope") == []
