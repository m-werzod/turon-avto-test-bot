"""Mutable runtime settings.

Anything an admin can toggle at runtime lives here as a key/value row rather than
in the environment: ``.env`` is deployment configuration and requires a restart,
whereas "pause the scheduler" must take effect immediately and survive one.
"""

from __future__ import annotations

from sqlalchemy import String, Text
from sqlalchemy.orm import Mapped, mapped_column

from bot.database.base import Base, IntPrimaryKeyMixin, TimestampMixin


class SettingKey:
    """Canonical setting names.

    A class of constants rather than an enum so repositories can accept plain
    strings while callers still get one authoritative spelling to import.
    """

    SCHEDULER_PAUSED = "scheduler_paused"
    POSTS_PER_DAY = "posts_per_day"
    SKIP_WEEKENDS = "skip_weekends"
    CONTENT_LANGUAGE = "content_language"
    LAST_IMPORT_AT = "last_import_at"
    LAST_IMPORT_SUMMARY = "last_import_summary"
    CURRENT_CYCLE_ID = "current_cycle_id"

    #: Defaults applied when a key has never been written.
    DEFAULTS: dict[str, str] = {  # noqa: RUF012 - simple constant mapping
        SCHEDULER_PAUSED: "false",
        POSTS_PER_DAY: "3",
        SKIP_WEEKENDS: "false",
        CONTENT_LANGUAGE: "uz",
    }


class Setting(IntPrimaryKeyMixin, TimestampMixin, Base):
    """A single key/value setting stored as text."""

    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)

    #: Serialised as text; typed access is provided by SettingsRepository.
    value: Mapped[str | None] = mapped_column(Text, nullable=True)

    def __repr__(self) -> str:
        return f"<Setting {self.key}={self.value!r}>"
