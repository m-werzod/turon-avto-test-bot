"""Question bank queries and idempotent upserts."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import func, select, update

from bot.database.models.question import Question
from bot.database.repositories.base import BaseRepository
from bot.utils.logging import get_logger

logger = get_logger(__name__)


@dataclass(slots=True)
class UpsertResult:
    """Outcome of importing a batch of questions."""

    created: int = 0
    updated: int = 0
    unchanged: int = 0
    skipped: int = 0

    @property
    def total(self) -> int:
        """Rows considered, whatever happened to them."""
        return self.created + self.updated + self.unchanged + self.skipped

    def merge(self, other: UpsertResult) -> None:
        """Fold another result into this one."""
        self.created += other.created
        self.updated += other.updated
        self.unchanged += other.unchanged
        self.skipped += other.skipped


class QuestionRepository(BaseRepository[Question]):
    """Reads and writes over the question bank."""

    model = Question

    async def get_by_external_id(self, source: str, external_id: str) -> Question | None:
        """Look a question up by its natural key."""
        stmt = select(Question).where(
            Question.source == source, Question.external_id == external_id
        )
        return await self.session.scalar(stmt)

    async def get_existing_map(self, source: str) -> dict[str, Question]:
        """Return every question of ``source`` keyed by ``external_id``.

        Imports load this once and then match in memory: one query instead of one
        per incoming row, which is the difference between a few seconds and
        several minutes over a 1200-row file.
        """
        stmt = select(Question).where(Question.source == source)
        result = await self.session.scalars(stmt)
        return {question.external_id: question for question in result}

    async def count_active(self, language: str | None = None) -> int:
        """Number of questions eligible for posting."""
        stmt = select(func.count()).select_from(Question).where(Question.is_active.is_(True))
        if language:
            stmt = stmt.where(Question.language == language)
        return int(await self.session.scalar(stmt) or 0)

    async def count_with_images(self) -> int:
        """How many active questions have a cached image."""
        stmt = (
            select(func.count())
            .select_from(Question)
            .where(Question.is_active.is_(True), Question.image_path.is_not(None))
        )
        return int(await self.session.scalar(stmt) or 0)

    async def list_missing_images(self, limit: int | None = None) -> list[Question]:
        """Active questions that reference a remote image but have no local copy.

        Drives the media backfill: an import can record image URLs quickly and let
        downloads happen afterwards without blocking the operator.
        """
        stmt = select(Question).where(
            Question.is_active.is_(True),
            Question.image_url.is_not(None),
            Question.image_path.is_(None),
        )
        if limit is not None:
            stmt = stmt.limit(limit)
        result = await self.session.scalars(stmt)
        return list(result)

    async def set_image(
        self, question_id: int, *, image_path: str | None, file_id: str | None = None
    ) -> None:
        """Record the cached image path and/or Telegram ``file_id``."""
        values: dict[str, object] = {"image_path": image_path}
        if file_id is not None:
            values["image_file_id"] = file_id
        await self.session.execute(
            update(Question).where(Question.id == question_id).values(**values)
        )
        await self.session.flush()

    async def set_file_id(self, question_id: int, file_id: str) -> None:
        """Cache the Telegram ``file_id`` after a successful upload.

        Later sends of the same image then cost no upload bandwidth at all.
        """
        await self.session.execute(
            update(Question).where(Question.id == question_id).values(image_file_id=file_id)
        )
        await self.session.flush()

    async def clear_file_id(self, question_id: int) -> None:
        """Forget a cached ``file_id`` that Telegram has rejected.

        ``file_id`` values can go stale; dropping ours forces the next send to
        re-upload from the local file rather than failing forever.
        """
        await self.session.execute(
            update(Question).where(Question.id == question_id).values(image_file_id=None)
        )
        await self.session.flush()

    async def upsert(
        self,
        *,
        source: str,
        external_id: str,
        text: str,
        options: list[str],
        correct_index: int,
        explanation: str | None = None,
        category: str | None = None,
        language: str = "uz",
        image_url: str | None = None,
        original_url: str | None = None,
        existing: Question | None = None,
    ) -> tuple[Question, str]:
        """Insert a question, or update it only when its content really changed.

        Args:
            source: Importer identity, part of the natural key.
            external_id: Stable id within that source.
            text: Question body.
            options: Exactly four answers, in display order.
            correct_index: Zero-based index of the right answer.
            explanation: Optional rationale shown after answering.
            category: Optional topic label.
            language: Content language code.
            image_url: Remote image, downloaded separately.
            original_url: Provenance link.
            existing: Pre-fetched row, when the caller already has one. Avoids a
                per-row SELECT during bulk import.

        Returns:
            The stored question and one of ``"created"``, ``"updated"`` or
            ``"unchanged"``.
        """
        content_hash = Question.compute_content_hash(text, options, correct_index, explanation)

        if existing is None:
            existing = await self.get_by_external_id(source, external_id)

        if existing is None:
            question = Question(
                source=source,
                external_id=external_id,
                text=text,
                options=options,
                correct_index=correct_index,
                explanation=explanation,
                category=category,
                language=language,
                image_url=image_url,
                original_url=original_url,
                content_hash=content_hash,
                is_active=True,
            )
            await self.add(question)
            return question, "created"

        # Content unchanged: leave updated_at alone so "recently changed" stays
        # meaningful, but still repair metadata that may have been missing before.
        if existing.content_hash == content_hash:
            metadata_changed = self.apply_updates(
                existing,
                category=category if category is not None else existing.category,
                image_url=image_url if image_url is not None else existing.image_url,
                original_url=original_url if original_url is not None else existing.original_url,
            )
            if metadata_changed:
                await self.session.flush()
            return existing, "unchanged"

        self.apply_updates(
            existing,
            text=text,
            options=options,
            correct_index=correct_index,
            explanation=explanation,
            category=category,
            language=language,
            original_url=original_url,
            content_hash=content_hash,
            is_active=True,
        )

        # A changed image URL invalidates both the local copy and the cached
        # file_id, so the media service re-downloads on the next send.
        if image_url is not None and image_url != existing.image_url:
            existing.image_url = image_url
            existing.image_path = None
            existing.image_file_id = None

        await self.session.flush()
        return existing, "updated"

    async def deactivate_missing(self, source: str, seen_external_ids: set[str]) -> int:
        """Deactivate questions of ``source`` absent from the latest import.

        Soft-deletes rather than removing them, so historical deliveries and
        statistics stay intact.

        Returns:
            Number of questions deactivated.
        """
        stmt = select(Question).where(Question.source == source, Question.is_active.is_(True))
        result = await self.session.scalars(stmt)
        deactivated = 0
        for question in result:
            if question.external_id not in seen_external_ids:
                question.is_active = False
                deactivated += 1
        if deactivated:
            await self.session.flush()
            logger.info("Deactivated %d question(s) no longer present in %s", deactivated, source)
        return deactivated

    async def sources(self) -> list[str]:
        """Distinct source names present in the bank."""
        stmt = select(Question.source).distinct().order_by(Question.source)
        result = await self.session.scalars(stmt)
        return list(result)
