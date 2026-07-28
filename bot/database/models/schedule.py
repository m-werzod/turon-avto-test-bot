"""Daily posting times."""

from __future__ import annotations

from datetime import time
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, Index, Time

from sqlalchemy.orm import Mapped, mapped_column

from bot.database.base import Base, IntPrimaryKeyMixin, TimestampMixin

if TYPE_CHECKING:  # pragma: no cover - typing only
    pass


class ScheduleSlot(IntPrimaryKeyMixin, TimestampMixin, Base):
    """One time of day at which a quiz is published.

    The admin picks one, two or three slots; each is stored as a naive local time
    and interpreted in the configured ``TIMEZONE`` (Asia/Tashkent by default),
    never in server local time.

    Slots live in the database rather than in an APScheduler job store, so the
    schedule is plain readable data: it survives a restart, a container rebuild
    and a database restore, and it can be inspected with ordinary SQL.
    """

    __tablename__ = "schedule_slots"

    #: Wall-clock time in the configured timezone, e.g. 08:00.
    run_at: Mapped[time] = mapped_column(Time(timezone=False), nullable=False, unique=True)

    is_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )

    __table_args__ = (Index("ix_schedule_slots_enabled_run_at", "is_enabled", "run_at"),)

    @property
    def label(self) -> str:
        """``HH:MM`` rendering used throughout the admin UI."""
        return self.run_at.strftime("%H:%M")

    def __repr__(self) -> str:
        return f"<ScheduleSlot {self.label} enabled={self.is_enabled}>"
