"""People who have interacted with the bot."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, String, func
from sqlalchemy.orm import Mapped, mapped_column

from bot.database.base import Base, IntPrimaryKeyMixin, TelegramId, TimestampMixin


class BotUser(IntPrimaryKeyMixin, TimestampMixin, Base):
    """A Telegram user, stored mainly to remember their language choice.

    ``is_admin`` is a cached convenience for display and statistics only.
    Authorisation is always decided against ``ADMIN_IDS`` from the environment,
    so a database write can never grant somebody the admin panel.
    """

    __tablename__ = "bot_users"

    telegram_id: Mapped[int] = mapped_column(
        TelegramId, nullable=False, unique=True, index=True
    )

    username: Mapped[str | None] = mapped_column(String(64), nullable=True)
    first_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    last_name: Mapped[str | None] = mapped_column(String(128), nullable=True)

    #: Interface language: ``uz`` or ``ru``.
    language: Mapped[str] = mapped_column(String(8), nullable=False, default="uz")

    is_admin: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )

    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    @property
    def full_name(self) -> str:
        """First and last name joined, falling back to the username or id."""
        parts = [part for part in (self.first_name, self.last_name) if part]
        if parts:
            return " ".join(parts)
        return self.username or str(self.telegram_id)

    def __repr__(self) -> str:
        return f"<BotUser telegram_id={self.telegram_id} lang={self.language}>"
