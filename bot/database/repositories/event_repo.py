"""Audit-log storage backing the in-Telegram Logs panel."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import delete, select

from bot.database.models.event_log import EventLog
from bot.database.repositories.base import BaseRepository


class EventRepository(BaseRepository[EventLog]):
    """Structured events an admin can read from their phone."""

    model = EventLog

    async def record(
        self,
        event: str,
        message: str,
        *,
        level: str = "INFO",
        payload: dict[str, Any] | None = None,
    ) -> EventLog:
        """Append an event.

        Args:
            event: One of :class:`~bot.database.models.event_log.EventType`.
            message: Human-readable summary shown in the Logs panel.
            level: Standard logging level name.
            payload: Optional structured context.

        Returns:
            The stored event.
        """
        entry = EventLog(event=event, message=message, level=level.upper(), payload=payload)
        return await self.add(entry)

    async def recent(
        self, *, limit: int = 20, level: str | None = None, event: str | None = None
    ) -> list[EventLog]:
        """Most recent events, newest first, optionally filtered."""
        stmt = select(EventLog).order_by(EventLog.created_at.desc(), EventLog.id.desc())
        if level:
            stmt = stmt.where(EventLog.level == level.upper())
        if event:
            stmt = stmt.where(EventLog.event == event)
        result = await self.session.scalars(stmt.limit(limit))
        return list(result)

    async def purge_older_than(self, days: int) -> int:
        """Delete events older than ``days``.

        Called by a nightly maintenance job. Without it this table grows forever
        on a long-running deployment.

        Returns:
            Number of rows deleted.
        """
        cutoff = datetime.now(UTC) - timedelta(days=days)
        result = await self.session.execute(delete(EventLog).where(EventLog.created_at < cutoff))
        await self.session.flush()
        return int(result.rowcount or 0)
