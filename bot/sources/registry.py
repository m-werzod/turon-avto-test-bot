"""Source discovery and construction.

One place decides which reader handles which file, so the import service and the
admin handlers never branch on file extensions themselves.
"""

from __future__ import annotations

from pathlib import Path

from bot.config.settings import PROJECT_ROOT
from bot.sources.base import QuestionSource, SourceError
from bot.sources.file_sources import (
    CsvQuestionSource,
    FileQuestionSource,
    JsonQuestionSource,
    XlsxQuestionSource,
)
from bot.utils.logging import get_logger

logger = get_logger(__name__)

#: Where an operator drops question files on the server.
DATA_DIR: Path = PROJECT_ROOT / "data"

#: Extension to reader. Keys are lower-case and include the leading dot.
_READERS: dict[str, type[FileQuestionSource]] = {
    ".json": JsonQuestionSource,
    ".csv": CsvQuestionSource,
    ".tsv": CsvQuestionSource,
    ".xlsx": XlsxQuestionSource,
    ".xlsm": XlsxQuestionSource,
}

#: Extensions the admin panel advertises as importable.
SUPPORTED_EXTENSIONS: tuple[str, ...] = tuple(sorted(_READERS))


def build_source(
    path: Path,
    *,
    name: str | None = None,
    default_language: str = "uz",
    strict: bool = False,
) -> QuestionSource:
    """Return the reader appropriate for ``path``.

    Args:
        path: File to import.
        name: Override the stored source identity.
        default_language: Language assumed for records that omit one.
        strict: Fail the import on the first invalid record.

    Returns:
        A ready-to-use source.

    Raises:
        SourceError: The extension is not supported.
    """
    reader = _READERS.get(path.suffix.lower())
    if reader is None:
        raise SourceError(
            f"Unsupported file type {path.suffix!r}. "
            f"Supported: {', '.join(SUPPORTED_EXTENSIONS)}"
        )
    return reader(path, name=name, default_language=default_language, strict=strict)


def discover_data_files(directory: Path | None = None) -> list[Path]:
    """List importable files in the data directory, newest first.

    Args:
        directory: Directory to scan. Defaults to ``<project>/data``.

    Returns:
        Matching files sorted by modification time, most recent first, so the
        file an operator just uploaded appears at the top of the admin list.
    """
    target = directory or DATA_DIR
    if not target.exists():
        logger.info("Data directory %s does not exist yet", target)
        return []

    files = [
        candidate
        for candidate in target.iterdir()
        if candidate.is_file() and candidate.suffix.lower() in _READERS
    ]
    return sorted(files, key=lambda item: item.stat().st_mtime, reverse=True)
