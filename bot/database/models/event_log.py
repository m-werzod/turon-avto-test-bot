"""Structured audit trail readable from inside Telegram.

Files under ``logs/`` are the operational record, but an admin on a phone cannot
tail them. Significant events are therefore mirrored into this table so the
"Logs" panel can render them, and so history survives log rotation.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from bot.database.base import Base, IntPrimaryKeyMixin, JSONType, TimestampMixin


class EventType:
    """Canonical ``event`` values, so filters do not rely on typed strings."""

    BOT_STARTED = "bot_started"
    BOT_STOPPED = "bot_stopped"
    SCHEDULER_STARTED = "scheduler_started"
    SCHEDULER_PAUSED = "scheduler_paused"
    SCHEDULER_RESUMED = "scheduler_resumed"
    SCHEDULE_UPDATED = "schedule_updated"
    QUIZ_SENT = "quiz_sent"
    QUIZ_FAILED = "quiz_failed"
    CYCLE_STARTED = "cycle_started"
    CYCLE_COMPLETED = "cycle_completed"
    IMPORT_STARTED = "import_started"
    IMPORT_FINISHED = "import_finished"
    IMPORT_FAILED = "import_failed"
    CHANNEL_CONNECTED = "channel_connected"
    CHANNEL_REMOVED = "channel_removed"
    CHANNEL_CHECK_FAILED = "channel_check_failed"
    BACKUP_CREATED = "backup_created"
    ERROR = "error"


class EventLog(IntPrimaryKeyMixin, TimestampMixin, Base):
    """One recorded event."""

    __tablename__ = "event_logs"

    #: Standard logging level name: INFO, WARNING, ERROR.
    level: Mapped[str] = mapped_column(String(16), nullable=False, default="INFO", index=True)

    #: One of :class:`EventType`.
    event: Mapped[str] = mapped_column(String(64), nullable=False, index=True)

    message: Mapped[str] = mapped_column(Text, nullable=False)

    #: Free-form structured context, e.g. ``{"question_id": 42, "channel": "@x"}``.
    payload: Mapped[dict[str, Any] | None] = mapped_column(JSONType, nullable=True)

    __table_args__ = (
        # The Logs panel reads newest-first, optionally filtered by level.
        Index("ix_event_logs_created_desc", "created_at"),
        Index("ix_event_logs_level_created", "level", "created_at"),
    )

    def __repr__(self) -> str:
        return f"<EventLog {self.level} {self.event}>"
