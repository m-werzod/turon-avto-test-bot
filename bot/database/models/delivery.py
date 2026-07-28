"""Per-channel outcome of a quiz post."""

from __future__ import annotations

import enum
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import BigInteger, DateTime, Enum as SAEnum, ForeignKey, Index, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from bot.database.base import Base, IntPrimaryKeyMixin, TimestampMixin

if TYPE_CHECKING:
    from bot.database.models.channel import Channel
    from bot.database.models.quiz_post import QuizPost


class DeliveryStatus(enum.StrEnum):
    """Lifecycle of a single channel delivery."""

    PENDING = "pending"
    SENT = "sent"
    FAILED = "failed"


class Delivery(IntPrimaryKeyMixin, TimestampMixin, Base):
    """The result of publishing one post to one channel.

    Recorded per channel so statistics can distinguish "the question was used"
    from "the question actually reached the audience", and so a single broken
    channel is visible instead of silently degrading the feed.
    """

    __tablename__ = "deliveries"

    post_id: Mapped[int] = mapped_column(
        ForeignKey("quiz_posts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    channel_id: Mapped[int] = mapped_column(
        ForeignKey("channels.id", ondelete="CASCADE"), nullable=False, index=True
    )

    status: Mapped[DeliveryStatus] = mapped_column(
        SAEnum(
            DeliveryStatus,
            native_enum=False,
            length=16,
            values_callable=lambda e: [m.value for m in e],
        ),
        nullable=False,
        default=DeliveryStatus.PENDING,
        index=True,
    )

    #: Message id of the quiz poll itself.
    poll_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    #: Message id of the image sent just before the poll, when there was one.
    photo_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    post: Mapped[QuizPost] = relationship(back_populates="deliveries")
    channel: Mapped[Channel] = relationship(back_populates="deliveries")

    __table_args__ = (
        # Drives "how many went out today" without scanning the whole table.
        Index("ix_deliveries_status_sent_at", "status", "sent_at"),
    )

    def __repr__(self) -> str:
        return f"<Delivery id={self.id} post={self.post_id} status={self.status}>"
