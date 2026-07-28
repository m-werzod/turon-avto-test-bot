"""The question bank."""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, CheckConstraint, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from bot.database.base import Base, IntPrimaryKeyMixin, JSONType, TimestampMixin
from bot.utils.text import REQUIRED_OPTION_COUNT, collapse_whitespace

if TYPE_CHECKING:
    from bot.database.models.quiz_post import QuizPost


class Question(IntPrimaryKeyMixin, TimestampMixin, Base):
    """A single driving-exam question with four options and one correct answer.

    Questions are imported once and served from the database forever after. The
    bot never reaches out to a content source while posting, so an unreachable
    source cannot interrupt the schedule.
    """

    __tablename__ = "questions"

    #: Stable identifier from the originating dataset. Together with ``source``
    #: this is what makes re-importing idempotent: an existing external_id is
    #: updated in place, never inserted twice.
    external_id: Mapped[str] = mapped_column(String(128), nullable=False)

    #: Name of the source that produced this row, e.g. ``"json:uz_official"``.
    source: Mapped[str] = mapped_column(String(64), nullable=False, index=True)

    language: Mapped[str] = mapped_column(String(8), nullable=False, default="uz", index=True)

    text: Mapped[str] = mapped_column(Text, nullable=False)

    #: Exactly ``REQUIRED_OPTION_COUNT`` answer strings, in display order.
    options: Mapped[list[str]] = mapped_column(JSONType, nullable=False)

    #: Zero-based index into ``options``.
    correct_index: Mapped[int] = mapped_column(Integer, nullable=False)

    explanation: Mapped[str | None] = mapped_column(Text, nullable=True)

    category: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)

    #: Remote image location, kept for re-download and provenance.
    image_url: Mapped[str | None] = mapped_column(Text, nullable=True)

    #: Path of the cached image relative to ``MEDIA_ROOT``. Populated once the
    #: media service has downloaded it; ``None`` means "text-only question".
    image_path: Mapped[str | None] = mapped_column(String(512), nullable=True)

    #: Telegram ``file_id`` of the last successful upload. Reusing it makes
    #: repeat sends instant and costs no bandwidth.
    image_file_id: Mapped[str | None] = mapped_column(String(256), nullable=True)

    original_url: Mapped[str | None] = mapped_column(Text, nullable=True)

    #: Hash of the semantic content. Lets an import detect a genuinely changed
    #: question without diffing every field by hand.
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)

    #: Soft-delete flag. Inactive questions are skipped by the picker but keep
    #: their delivery history intact.
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true", index=True
    )

    posts: Mapped[list[QuizPost]] = relationship(
        back_populates="question",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    __table_args__ = (
        # Idempotent imports hinge on this pair being unique.
        Index("uq_questions_source_external_id", "source", "external_id", unique=True),
        CheckConstraint(
            f"correct_index >= 0 AND correct_index < {REQUIRED_OPTION_COUNT}",
            name="correct_index_in_range",
        ),
        # Partial-friendly composite index for the "next unseen question" query.
        Index("ix_questions_active_language", "is_active", "language"),
    )

    @staticmethod
    def compute_content_hash(
        text: str,
        options: list[str],
        correct_index: int,
        explanation: str | None = None,
    ) -> str:
        """Return a stable SHA-256 digest of a question's meaning.

        Whitespace is collapsed first so that cosmetic reformatting upstream does
        not present as a content change and trigger a pointless update.

        Args:
            text: Question body.
            options: Answer options in display order.
            correct_index: Index of the correct option.
            explanation: Optional explanation text.

        Returns:
            Hex-encoded SHA-256 digest.
        """
        parts = [
            collapse_whitespace(text),
            *(collapse_whitespace(option) for option in options),
            str(correct_index),
            collapse_whitespace(explanation or ""),
        ]
        joined = "\x1f".join(parts)
        return hashlib.sha256(joined.encode("utf-8")).hexdigest()

    @property
    def correct_option(self) -> str:
        """The text of the correct answer."""
        return self.options[self.correct_index]

    @property
    def has_image(self) -> bool:
        """Whether an image is available to send alongside the poll."""
        return bool(self.image_file_id or self.image_path)

    def __repr__(self) -> str:
        return f"<Question id={self.id} source={self.source!r} external_id={self.external_id!r}>"
