"""A question claimed for publication within a cycle."""

from __future__ import annotations

import enum
from typing import TYPE_CHECKING

from sqlalchemy import Enum as SAEnum
from sqlalchemy import ForeignKey, Index, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from bot.database.base import Base, IntPrimaryKeyMixin, TimestampMixin

if TYPE_CHECKING:
    from bot.database.models.cycle import Cycle
    from bot.database.models.delivery import Delivery
    from bot.database.models.question import Question


class PostTrigger(enum.StrEnum):
    """What caused a post to be created."""

    SCHEDULED = "scheduled"
    MANUAL = "manual"
    CATCHUP = "catchup"


class QuizPost(IntPrimaryKeyMixin, TimestampMixin, Base):
    """One question, used once, in one cycle.

    Creating a row here is how a question is *claimed*. The unique constraint on
    ``(cycle_id, question_id)`` is the hard guarantee that no question repeats
    within a cycle — enforced by the database, not by application logic that a
    race or a restart could sidestep.

    A post is separate from a :class:`~bot.database.models.delivery.Delivery`
    because one claimed question fans out to every connected channel, and a
    failure to reach one channel must not put the question back in the pool.
    """

    __tablename__ = "quiz_posts"

    cycle_id: Mapped[int] = mapped_column(
        ForeignKey("cycles.id", ondelete="CASCADE"), nullable=False, index=True
    )
    question_id: Mapped[int] = mapped_column(
        ForeignKey("questions.id", ondelete="CASCADE"), nullable=False, index=True
    )

    trigger: Mapped[PostTrigger] = mapped_column(
        SAEnum(PostTrigger, native_enum=False, length=16, values_callable=lambda e: [m.value for m in e]),
        nullable=False,
        default=PostTrigger.SCHEDULED,
    )

    cycle: Mapped[Cycle] = relationship(back_populates="posts")
    question: Mapped[Question] = relationship(back_populates="posts")
    deliveries: Mapped[list[Delivery]] = relationship(
        back_populates="post",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    __table_args__ = (
        UniqueConstraint("cycle_id", "question_id", name="uq_quiz_posts_cycle_question"),
        Index("ix_quiz_posts_cycle_created", "cycle_id", "created_at"),
    )

    def __repr__(self) -> str:
        return f"<QuizPost id={self.id} cycle={self.cycle_id} question={self.question_id}>"
