"""Posting cycles.

A *cycle* is one full pass over the question bank. Inside a cycle every question
is posted at most once; when none are left the cycle closes and the next one
opens automatically. This is the mechanism behind the "never repeat a question
until all 1225 have been posted" requirement.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, Integer, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from bot.database.base import Base, IntPrimaryKeyMixin, OwnerMixin, TimestampMixin

if TYPE_CHECKING:
    from bot.database.models.quiz_post import QuizPost


class Cycle(IntPrimaryKeyMixin, OwnerMixin, TimestampMixin, Base):
    """One complete pass through the question bank."""

    __tablename__ = "cycles"

    #: 1-based, monotonically increasing. Shown to the admin as "Cycle 3".
    number: Mapped[int] = mapped_column(Integer, nullable=False, index=True)

    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    #: ``None`` while the cycle is the active one.
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    #: Size of the active question bank when the cycle opened. Kept as a snapshot
    #: so progress stays meaningful even if questions are imported mid-cycle.
    questions_total: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    __table_args__ = (
        # Cycle numbering restarts per owner, so it is only unique within one.
        UniqueConstraint("owner_id", "number", name="owner_cycle_number"),
    )

    posts: Mapped[list[QuizPost]] = relationship(
        back_populates="cycle",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    @property
    def is_open(self) -> bool:
        """Whether this cycle is still accepting posts."""
        return self.completed_at is None

    def __repr__(self) -> str:
        state = "open" if self.is_open else "closed"
        return f"<Cycle number={self.number} {state}>"
