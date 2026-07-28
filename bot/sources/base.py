"""The question-source contract and the shared record parser.

The bot deliberately knows nothing about *where* questions come from. A source
yields :class:`RawQuestion` records; the import service persists them. Adding a
new provider means implementing one class and registering it, with no change to
the scheduler, the poll builder or the database.

All field-name tolerance and validation lives here rather than in each format
reader, so a JSON file, a CSV and a spreadsheet accept exactly the same shapes
and report exactly the same errors.
"""

from __future__ import annotations

import abc
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from bot.utils.text import (
    POLL_EXPLANATION_LIMIT,
    POLL_OPTION_LIMIT,
    POLL_QUESTION_LIMIT,
    collapse_whitespace,
    normalize_for_poll,
)

#: Every question must offer exactly this many options — Telegram quiz polls in
#: this project are always four-way, and a bank with ragged option counts would
#: fail at send time instead of at import time.
REQUIRED_OPTIONS = 4

# Accepted spellings for each logical field. Real-world question banks are
# exported by many different tools; normalising here costs one dict and saves
# every operator a manual column-renaming pass.
_ID_KEYS = ("external_id", "id", "question_id", "test_id", "number", "no")
_TEXT_KEYS = ("text", "question", "question_text", "savol", "vopros", "title")
_OPTIONS_KEYS = ("options", "answers", "variants", "choices", "javoblar")
_EXPLANATION_KEYS = ("explanation", "comment", "izoh", "description", "note")
_CATEGORY_KEYS = ("category", "topic", "mavzu", "section", "tema")
_IMAGE_KEYS = ("image_url", "image", "photo", "picture", "rasm", "img")
_URL_KEYS = ("original_url", "url", "source_url", "link", "manba")
_LANGUAGE_KEYS = ("language", "lang", "til")

#: Zero-based index of the correct option.
_CORRECT_INDEX_KEYS = ("correct_index", "answer_index", "correct_idx")
#: One-based option number — what a spreadsheet author naturally writes.
_CORRECT_NUMBER_KEYS = ("correct", "correct_option", "answer", "togri_javob", "correct_no")
#: The correct answer given as text, matched against the options.
_CORRECT_TEXT_KEYS = ("correct_answer", "correct_text", "answer_text")

#: Per-option column names used by flat formats: option1..option4, a..d, etc.
_INDEXED_OPTION_PREFIXES = ("option", "answer", "variant", "choice", "opt", "javob")
_LETTER_OPTION_KEYS = ("a", "b", "c", "d")


class SourceError(RuntimeError):
    """A source could not be read at all (missing file, malformed container)."""


class QuestionValidationError(ValueError):
    """A single record was rejected.

    Carries the offending record's position so an operator can find it in a
    1200-row file instead of hunting blindly.
    """

    def __init__(self, message: str, *, location: str | None = None) -> None:
        self.location = location
        super().__init__(f"{location}: {message}" if location else message)


@dataclass(slots=True)
class RawQuestion:
    """One validated question, ready to be persisted.

    Text fields are already collapsed and clamped to the Bot API limits, so
    nothing downstream has to re-check lengths before building a poll.
    """

    external_id: str
    text: str
    options: list[str]
    correct_index: int
    explanation: str | None = None
    category: str | None = None
    language: str = "uz"
    image_url: str | None = None
    original_url: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def correct_option(self) -> str:
        """Text of the correct answer."""
        return self.options[self.correct_index]


class QuestionSource(abc.ABC):
    """Something that can produce questions.

    Implementations stream records instead of returning a list, so importing a
    large bank never holds the whole file in memory at once.
    """

    #: Stable identifier stored on every question this source produces. Combined
    #: with ``external_id`` it forms the natural key that makes re-import
    #: idempotent, so changing it creates duplicates — treat it as permanent.
    name: str

    @abc.abstractmethod
    async def fetch(self) -> AsyncIterator[RawQuestion]:
        """Yield every question this source offers.

        Raises:
            SourceError: The source is unreadable as a whole.
        """
        raise NotImplementedError

    @abc.abstractmethod
    async def count_estimate(self) -> int | None:
        """Approximate number of records, for progress reporting.

        Returns:
            A count, or ``None`` when it cannot be known cheaply.
        """
        raise NotImplementedError

    def __repr__(self) -> str:
        return f"<{type(self).__name__} name={self.name!r}>"


def _first_present(record: dict[str, Any], keys: tuple[str, ...]) -> Any:
    """Return the first non-empty value among ``keys``."""
    for key in keys:
        if key in record:
            value = record[key]
            if value is not None and str(value).strip() != "":
                return value
    return None


def _normalize_keys(record: dict[str, Any]) -> dict[str, Any]:
    """Lower-case and trim keys so header casing never matters."""
    return {str(key).strip().lower(): value for key, value in record.items()}


def _extract_options(record: dict[str, Any]) -> list[str]:
    """Collect answer options from any supported layout.

    Handles a real list under ``options``, a delimited string, numbered columns
    (``option1``…``option4``) and lettered columns (``a``…``d``).
    """
    raw = _first_present(record, _OPTIONS_KEYS)

    if isinstance(raw, list | tuple):
        return [str(item) for item in raw if str(item).strip()]

    if isinstance(raw, str):
        # A single cell holding all options, separated by | or newlines.
        for separator in ("|", "\n", ";"):
            if separator in raw:
                parts = [part for part in raw.split(separator) if part.strip()]
                if len(parts) >= 2:
                    return parts

    # Numbered columns: option1, answer2, variant_3 ...
    numbered: list[str] = []
    for index in range(1, REQUIRED_OPTIONS + 1):
        for prefix in _INDEXED_OPTION_PREFIXES:
            for candidate in (f"{prefix}{index}", f"{prefix}_{index}"):
                if candidate in record and str(record[candidate]).strip():
                    numbered.append(str(record[candidate]))
                    break
            else:
                continue
            break
    if numbered:
        return numbered

    # Lettered columns: a, b, c, d
    lettered = [
        str(record[letter])
        for letter in _LETTER_OPTION_KEYS
        if letter in record and str(record[letter]).strip()
    ]
    if lettered:
        return lettered

    return []


def _resolve_correct_index(record: dict[str, Any], options: list[str], location: str) -> int:
    """Work out which option is correct, from whichever field was supplied.

    Three spellings are accepted, and they mean different things — guessing
    between them would silently mark the wrong answer correct, so each is
    explicit:

    * ``correct_index`` — zero-based index.
    * ``correct`` / ``answer`` — one-based option number, what a spreadsheet
      author writes.
    * ``correct_answer`` — the answer text, matched against the options.

    Raises:
        QuestionValidationError: No usable field, or a value out of range.
    """
    zero_based = _first_present(record, _CORRECT_INDEX_KEYS)
    if zero_based is not None:
        try:
            index = int(str(zero_based).strip())
        except ValueError as exc:
            raise QuestionValidationError(
                f"correct_index must be a number, got {zero_based!r}", location=location
            ) from exc
        if not 0 <= index < len(options):
            raise QuestionValidationError(
                f"correct_index {index} is out of range for {len(options)} options",
                location=location,
            )
        return index

    one_based = _first_present(record, _CORRECT_NUMBER_KEYS)
    if one_based is not None:
        raw = str(one_based).strip()
        # A letter is also common: "B" means the second option.
        if len(raw) == 1 and raw.lower() in _LETTER_OPTION_KEYS:
            return _LETTER_OPTION_KEYS.index(raw.lower())
        try:
            number = int(raw)
        except ValueError:
            # Not a number or a letter — fall through to a text match.
            number = None  # type: ignore[assignment]
        if number is not None:
            if not 1 <= number <= len(options):
                raise QuestionValidationError(
                    f"correct option {number} is out of range for {len(options)} options "
                    f"(this field is 1-based; use 'correct_index' for 0-based)",
                    location=location,
                )
            return number - 1

    text_answer = _first_present(record, _CORRECT_TEXT_KEYS) or _first_present(
        record, _CORRECT_NUMBER_KEYS
    )
    if text_answer is not None:
        needle = collapse_whitespace(str(text_answer)).casefold()
        for index, option in enumerate(options):
            if collapse_whitespace(option).casefold() == needle:
                return index
        raise QuestionValidationError(
            f"correct answer {text_answer!r} does not match any option", location=location
        )

    raise QuestionValidationError(
        "no correct answer given (expected one of: correct_index, correct, correct_answer)",
        location=location,
    )


def parse_record(
    record: dict[str, Any],
    *,
    location: str,
    default_language: str = "uz",
    fallback_id: str | None = None,
) -> RawQuestion:
    """Turn one raw mapping into a validated :class:`RawQuestion`.

    Args:
        record: Raw key/value data from a file or API.
        location: Where this record came from, e.g. ``"row 42"``. Used in errors.
        default_language: Language to assume when the record does not say.
        fallback_id: Identifier to use when the record carries none. Supplying
            the row number here keeps import idempotent for files that have no
            id column, since the same row maps to the same question next time.

    Returns:
        A validated question with text already clamped to Bot API limits.

    Raises:
        QuestionValidationError: The record is unusable.
    """
    normalized = _normalize_keys(record)

    text_raw = _first_present(normalized, _TEXT_KEYS)
    if text_raw is None:
        raise QuestionValidationError("question text is missing", location=location)
    text = normalize_for_poll(str(text_raw), POLL_QUESTION_LIMIT)
    if not text:
        raise QuestionValidationError("question text is empty", location=location)

    options_raw = _extract_options(normalized)
    if len(options_raw) != REQUIRED_OPTIONS:
        raise QuestionValidationError(
            f"expected exactly {REQUIRED_OPTIONS} options, found {len(options_raw)}",
            location=location,
        )

    options = [normalize_for_poll(option, POLL_OPTION_LIMIT) for option in options_raw]
    if any(not option for option in options):
        raise QuestionValidationError("one or more options are empty", location=location)
    if len({option.casefold() for option in options}) != REQUIRED_OPTIONS:
        raise QuestionValidationError("options contain duplicates", location=location)

    correct_index = _resolve_correct_index(normalized, options, location)

    identifier = _first_present(normalized, _ID_KEYS)
    external_id = str(identifier).strip() if identifier is not None else fallback_id
    if not external_id:
        raise QuestionValidationError(
            "question id is missing and no fallback was provided", location=location
        )

    explanation_raw = _first_present(normalized, _EXPLANATION_KEYS)
    explanation = (
        normalize_for_poll(str(explanation_raw), POLL_EXPLANATION_LIMIT)
        if explanation_raw is not None
        else None
    )

    category_raw = _first_present(normalized, _CATEGORY_KEYS)
    category = collapse_whitespace(str(category_raw))[:128] if category_raw is not None else None

    image_raw = _first_present(normalized, _IMAGE_KEYS)
    image_url = str(image_raw).strip() if image_raw is not None else None

    url_raw = _first_present(normalized, _URL_KEYS)
    original_url = str(url_raw).strip() if url_raw is not None else None

    language_raw = _first_present(normalized, _LANGUAGE_KEYS)
    language = (
        str(language_raw).strip().lower()[:8] if language_raw is not None else default_language
    )

    return RawQuestion(
        external_id=external_id,
        text=text,
        options=options,
        correct_index=correct_index,
        explanation=explanation or None,
        category=category or None,
        language=language,
        image_url=image_url or None,
        original_url=original_url or None,
    )
