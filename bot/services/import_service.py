"""Importing a question bank into the database.

Import is deliberately idempotent: re-running it over the same file inserts
nothing new, and running it over an updated file inserts only what is genuinely
new. That is what makes the admin's "Update tests" button safe to press twice.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from bot.database.models.event_log import EventType
from bot.database.models.setting import SettingKey
from bot.database.repositories import (
    EventRepository,
    QuestionRepository,
    SettingsRepository,
    UpsertResult,
)
from bot.services.media_service import MediaError, MediaResult, MediaService
from bot.sources.base import QuestionSource, SourceError
from bot.utils.logging import get_logger

logger = get_logger(__name__)

#: Rows persisted between flushes during a bulk import.
_FLUSH_EVERY = 100

ProgressCallback = Callable[[str, int, int], Awaitable[None] | None]


@dataclass(slots=True)
class ImportReport:
    """Everything the admin is told after an import."""

    source_name: str
    result: UpsertResult = field(default_factory=UpsertResult)
    images_downloaded: int = 0
    images_failed: int = 0
    validation_errors: list[str] = field(default_factory=list)
    deactivated: int = 0
    total_in_bank: int = 0
    duration_seconds: float = 0.0

    @property
    def has_changes(self) -> bool:
        """Whether anything actually changed."""
        return bool(self.result.created or self.result.updated or self.deactivated)

    def summary_line(self) -> str:
        """One-line summary for logs and the settings table."""
        return (
            f"{self.source_name}: +{self.result.created} new, "
            f"~{self.result.updated} updated, ={self.result.unchanged} unchanged, "
            f"!{self.result.skipped} skipped, {self.images_downloaded} images"
        )


class ImportService:
    """Streams a source into the database and fetches its images."""

    def __init__(self, media: MediaService) -> None:
        self.media = media

    async def import_source(
        self,
        session: AsyncSession,
        source: QuestionSource,
        *,
        download_images: bool = True,
        deactivate_missing: bool = False,
        on_progress: ProgressCallback | None = None,
    ) -> ImportReport:
        """Import every question a source offers.

        Args:
            session: Open session; the caller owns the transaction.
            source: Where questions come from.
            download_images: Fetch and cache referenced images afterwards.
            deactivate_missing: Soft-delete stored questions the source no longer
                lists. Off by default, because importing a *partial* file would
                otherwise silently disable most of the bank.
            on_progress: Optional ``(stage, done, total)`` callback for live
                progress in the admin chat.

        Returns:
            What happened.

        Raises:
            SourceError: The source could not be read at all.
        """
        started = datetime.now(UTC)
        questions = QuestionRepository(session)
        settings_repo = SettingsRepository(session)
        events = EventRepository(session)

        await events.record(
            EventType.IMPORT_STARTED,
            f"Import started from {source.name}",
            payload={"source": source.name},
        )
        logger.info("Import started from %s", source.name)

        estimate = await source.count_estimate() or 0
        report = ImportReport(source_name=source.name)

        # One query for the whole source, then match in memory: 1200 individual
        # SELECTs would dominate the runtime of an otherwise trivial import.
        existing = await questions.get_existing_map(source.name)

        seen_ids: set[str] = set()
        image_urls: list[str] = []
        processed = 0

        try:
            async for raw in source.fetch():
                processed += 1
                seen_ids.add(raw.external_id)

                _, action = await questions.upsert(
                    source=source.name,
                    external_id=raw.external_id,
                    text=raw.text,
                    options=raw.options,
                    correct_index=raw.correct_index,
                    explanation=raw.explanation,
                    category=raw.category,
                    language=raw.language,
                    image_url=raw.image_url,
                    original_url=raw.original_url,
                    existing=existing.get(raw.external_id),
                )
                setattr(report.result, action, getattr(report.result, action) + 1)

                if raw.image_url:
                    image_urls.append(raw.image_url)

                if processed % _FLUSH_EVERY == 0:
                    await session.flush()
                    if on_progress:
                        outcome = on_progress("questions", processed, estimate)
                        if outcome is not None:
                            await outcome
        except SourceError:
            await events.record(
                EventType.IMPORT_FAILED,
                f"Import from {source.name} failed while reading the source",
                level="ERROR",
                payload={"source": source.name},
            )
            raise

        await session.flush()

        # Validation failures collected by the reader, surfaced to the admin.
        reader_errors = getattr(source, "errors", [])
        if reader_errors:
            report.result.skipped += len(reader_errors)
            report.validation_errors = list(reader_errors)

        if deactivate_missing and seen_ids:
            report.deactivated = await questions.deactivate_missing(source.name, seen_ids)

        if download_images and image_urls:
            await self._fetch_images(session, questions, image_urls, report, on_progress)

        report.total_in_bank = await questions.count_active()
        report.duration_seconds = (datetime.now(UTC) - started).total_seconds()

        await settings_repo.set_raw(SettingKey.LAST_IMPORT_AT, datetime.now(UTC).isoformat())
        await settings_repo.set_raw(SettingKey.LAST_IMPORT_SUMMARY, report.summary_line())

        await events.record(
            EventType.IMPORT_FINISHED,
            report.summary_line(),
            payload={
                "source": source.name,
                "created": report.result.created,
                "updated": report.result.updated,
                "unchanged": report.result.unchanged,
                "skipped": report.result.skipped,
                "images": report.images_downloaded,
                "seconds": round(report.duration_seconds, 1),
            },
        )
        logger.info(
            "Import finished in %.1fs — %s", report.duration_seconds, report.summary_line()
        )
        return report

    async def _fetch_images(
        self,
        session: AsyncSession,
        questions: QuestionRepository,
        urls: list[str],
        report: ImportReport,
        on_progress: ProgressCallback | None,
    ) -> None:
        """Download referenced images and attach them to their questions.

        A failed image never fails the import: the question still posts, just
        without a picture, which is far better than losing it entirely.
        """

        async def progress(done: int, total: int) -> None:
            if on_progress:
                outcome = on_progress("images", done, total)
                if outcome is not None:
                    await outcome

        results = await self.media.download_many(urls, on_progress=progress)

        # Map each successfully cached URL onto every question referencing it.
        pending = await questions.list_missing_images()
        by_url: dict[str, list[int]] = {}
        for question in pending:
            if question.image_url:
                by_url.setdefault(question.image_url, []).append(question.id)

        for url, outcome in results.items():
            if isinstance(outcome, MediaResult):
                report.images_downloaded += 1
                for question_id in by_url.get(url, []):
                    await questions.set_image(question_id, image_path=outcome.relative_path)
            else:
                report.images_failed += 1

        await session.flush()

        if report.images_failed:
            logger.warning(
                "%d image(s) could not be downloaded; those questions will post "
                "as text-only polls",
                report.images_failed,
            )

    async def backfill_images(
        self, session: AsyncSession, *, limit: int | None = None
    ) -> tuple[int, int]:
        """Retry images that were never successfully cached.

        Lets a transient outage during import be repaired later without
        re-running the whole thing.

        Returns:
            Counts of images downloaded and failed.
        """
        questions = QuestionRepository(session)
        pending = await questions.list_missing_images(limit=limit)
        if not pending:
            return 0, 0

        urls = [question.image_url for question in pending if question.image_url]
        results = await self.media.download_many(urls)

        downloaded = 0
        failed = 0
        for question in pending:
            if not question.image_url:
                continue
            outcome = results.get(question.image_url)
            if isinstance(outcome, MediaResult):
                await questions.set_image(question.id, image_path=outcome.relative_path)
                downloaded += 1
            else:
                failed += 1

        await session.flush()
        logger.info("Image backfill: %d downloaded, %d failed", downloaded, failed)
        return downloaded, failed
