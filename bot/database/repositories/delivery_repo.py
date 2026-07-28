"""Delivery records and the counters the statistics panel reads."""

from __future__ import annotations

from datetime import UTC, date, datetime, time, tzinfo
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import joinedload

from bot.database.models.channel import Channel
from bot.database.models.delivery import Delivery, DeliveryStatus
from bot.database.models.quiz_post import QuizPost
from bot.database.repositories.base import BaseRepository


class DeliveryRepository(BaseRepository[Delivery]):
    """Per-channel send outcomes."""

    model = Delivery

    @staticmethod
    def _for_owner(stmt: Any, owner_id: int | None) -> Any:
        """Restrict a delivery query to one owner's channels.

        A delivery has no owner of its own — it is reached through the channel it
        was sent to, which does. Passing ``None`` leaves the query
        installation-wide, which only the maintenance job wants.
        """
        if owner_id is None:
            return stmt
        return stmt.join(Channel, Delivery.channel_id == Channel.id).where(
            Channel.owner_id == owner_id
        )

    async def create_pending(self, post_id: int, channel_id: int) -> Delivery:
        """Record an attempt before it is made.

        Written up-front so a crash mid-send leaves visible evidence rather than
        a silent gap.
        """
        delivery = Delivery(post_id=post_id, channel_id=channel_id, status=DeliveryStatus.PENDING)
        return await self.add(delivery)

    async def mark_sent(
        self,
        delivery: Delivery,
        *,
        poll_message_id: int | None,
        photo_message_id: int | None = None,
    ) -> None:
        """Record a successful publication."""
        delivery.status = DeliveryStatus.SENT
        delivery.poll_message_id = poll_message_id
        delivery.photo_message_id = photo_message_id
        delivery.sent_at = datetime.now(UTC)
        delivery.error_message = None
        await self.session.flush()

    async def mark_failed(self, delivery: Delivery, error: str) -> None:
        """Record a failed publication with its reason."""
        delivery.status = DeliveryStatus.FAILED
        delivery.error_message = error[:2000]
        await self.session.flush()

    async def count_sent(self, owner_id: int | None = None) -> int:
        """Total successful deliveries across all time and channels."""
        stmt = (
            select(func.count()).select_from(Delivery).where(Delivery.status == DeliveryStatus.SENT)
        )
        return int(await self.session.scalar(self._for_owner(stmt, owner_id)) or 0)

    async def count_sent_on(self, day: date, tz: tzinfo, owner_id: int | None = None) -> int:
        """Successful deliveries during a local calendar day.

        The window is built in ``tz`` and converted to UTC, so "today" means the
        admin's day in Tashkent rather than the server's UTC day.
        """
        start_local = datetime.combine(day, time.min, tzinfo=tz)
        end_local = datetime.combine(day, time.max, tzinfo=tz)
        stmt = (
            select(func.count())
            .select_from(Delivery)
            .where(
                Delivery.status == DeliveryStatus.SENT,
                Delivery.sent_at >= start_local.astimezone(UTC),
                Delivery.sent_at <= end_local.astimezone(UTC),
            )
        )
        return int(await self.session.scalar(self._for_owner(stmt, owner_id)) or 0)

    async def count_failed(self, owner_id: int | None = None) -> int:
        """Total failed deliveries."""
        stmt = (
            select(func.count())
            .select_from(Delivery)
            .where(Delivery.status == DeliveryStatus.FAILED)
        )
        return int(await self.session.scalar(self._for_owner(stmt, owner_id)) or 0)

    async def last_sent(self, owner_id: int | None = None) -> Delivery | None:
        """Most recent successful delivery, with its post and question loaded."""
        stmt = (
            select(Delivery)
            .where(Delivery.status == DeliveryStatus.SENT)
            .options(
                joinedload(Delivery.post).joinedload(QuizPost.question),
                joinedload(Delivery.channel),
            )
            .order_by(Delivery.sent_at.desc())
            .limit(1)
        )
        return await self.session.scalar(self._for_owner(stmt, owner_id))

    async def recent(self, limit: int = 10, owner_id: int | None = None) -> list[Delivery]:
        """Recent deliveries of any status, newest first."""
        stmt = (
            select(Delivery)
            .options(
                joinedload(Delivery.post).joinedload(QuizPost.question),
                joinedload(Delivery.channel),
            )
            .order_by(Delivery.created_at.desc())
            .limit(limit)
        )
        result = await self.session.scalars(self._for_owner(stmt, owner_id))
        return list(result.unique())
