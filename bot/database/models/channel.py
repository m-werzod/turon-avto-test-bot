"""Connected Telegram channels."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, DateTime, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from bot.database.base import (
    Base,
    IntPrimaryKeyMixin,
    OwnerMixin,
    TelegramId,
    TimestampMixin,
)

if TYPE_CHECKING:
    from bot.database.models.delivery import Delivery


class Channel(IntPrimaryKeyMixin, OwnerMixin, TimestampMixin, Base):
    """A channel the bot publishes quizzes to.

    Several channels can be active at once; a scheduled post is broadcast to
    every active one.
    """

    __tablename__ = "channels"

    #: Numeric Telegram chat id. Authoritative — usernames can be changed by the
    #: channel owner at any time, the id cannot.
    chat_id: Mapped[int] = mapped_column(TelegramId, nullable=False, unique=True, index=True)

    #: Cached ``@username`` when the channel is public, for display only.
    username: Mapped[str | None] = mapped_column(String(64), nullable=True)

    title: Mapped[str | None] = mapped_column(String(256), nullable=True)

    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true", index=True
    )

    #: Telegram id of the admin who connected it, for the audit trail.
    added_by: Mapped[int | None] = mapped_column(TelegramId, nullable=True)

    #: When the permission check last passed.
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    #: Reason recorded by the last failed verification, shown to the admin.
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    deliveries: Mapped[list[Delivery]] = relationship(
        back_populates="channel",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    @property
    def display_name(self) -> str:
        """Best available human label for logs and admin messages."""
        return self.username or self.title or str(self.chat_id)

    def __repr__(self) -> str:
        return f"<Channel id={self.id} chat_id={self.chat_id} active={self.is_active}>"
