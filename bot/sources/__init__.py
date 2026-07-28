"""Pluggable question sources.

The bot is deliberately agnostic about where questions come from. Anything that
implements :class:`~bot.sources.base.QuestionSource` and yields
:class:`~bot.sources.base.RawQuestion` records can feed it, and nothing in the
scheduler, poll builder or database changes when a new provider is added.
"""

from bot.sources.base import (
    QuestionSource,
    QuestionValidationError,
    RawQuestion,
    SourceError,
    parse_record,
)
from bot.sources.file_sources import (
    CsvQuestionSource,
    FileQuestionSource,
    JsonQuestionSource,
    XlsxQuestionSource,
)
from bot.sources.registry import (
    DATA_DIR,
    SUPPORTED_EXTENSIONS,
    build_source,
    discover_data_files,
)

__all__ = [
    "DATA_DIR",
    "SUPPORTED_EXTENSIONS",
    "CsvQuestionSource",
    "FileQuestionSource",
    "JsonQuestionSource",
    "QuestionSource",
    "QuestionValidationError",
    "RawQuestion",
    "SourceError",
    "XlsxQuestionSource",
    "build_source",
    "discover_data_files",
    "parse_record",
]
