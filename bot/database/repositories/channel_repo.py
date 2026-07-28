"""Connected channel storage."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select

from bot.database.models.channel import Channel
from bot.database.repositories.base import OwnedRepository


class ChannelAlreadyConnectedError(RuntimeError):
    """The chat is already driven by a different owner.

    ``chat_id`` is globally unique: two owners posting to one channel would
    double-post every question. Raised so the handler can say who to ask rather
    than surfacing a raw IntegrityError.
    """


class ChannelRepository(OwnedRepository[Channel]):
    """Reads and writes over connected channels."""

    model = Channel

    async def get_by_chat_id(self, chat_id: int) -> Channel | None:
        """Look a channel up by its Telegram chat id."""
        return await self.session.scalar(
            self.owned(select(Channel).where(Channel.chat_id == chat_id))
        )

    async def list_active(self) -> list[Channel]:
        """Every channel a scheduled post should be broadcast to."""
        stmt = self.owned(select(Channel).where(Channel.is_active.is_(True))).order_by(Channel.id)
        result = await self.session.scalars(stmt)
        return list(result)

    async def upsert(
        self,
        *,
        chat_id: int,
        username: str | None,
        title: str | None,
        added_by: int | None = None,
    ) -> tuple[Channel, bool]:
        """Connect a channel, or refresh and re-activate an existing one.

        Reconnecting a previously removed channel revives the original row, which
        keeps its delivery history attached instead of orphaning it.

        Returns:
            The channel and whether it was newly created.

        Raises:
            ChannelAlreadyConnectedError: Another owner already drives this chat.
        """
        channel = await self.get_by_chat_id(chat_id)
        now = datetime.now(UTC)

        if channel is None:
            # Scoped lookups cannot see another owner's row, so check globally
            # before inserting — otherwise this hits the unique index and
            # surfaces as an IntegrityError the handler cannot explain.
            taken = await self.session.scalar(select(Channel).where(Channel.chat_id == chat_id))
            if taken is not None:
                raise ChannelAlreadyConnectedError(
                    f"Channel {chat_id} is already connected by another user"
                )

            channel = Channel(
                chat_id=chat_id,
                username=username,
                title=title,
                added_by=added_by,
                is_active=True,
                verified_at=now,
                last_error=None,
            )
            await self.add(channel)
            return channel, True

        self.apply_updates(
            channel,
            username=username,
            title=title,
            is_active=True,
            verified_at=now,
            last_error=None,
        )
        if added_by is not None and channel.added_by is None:
            channel.added_by = added_by
        await self.session.flush()
        return channel, False

    async def mark_verified(self, channel: Channel) -> None:
        """Record a successful permission check."""
        channel.verified_at = datetime.now(UTC)
        channel.last_error = None
        await self.session.flush()

    async def mark_failed(self, channel: Channel, reason: str) -> None:
        """Record why the last permission check or send failed."""
        channel.last_error = reason
        await self.session.flush()

    async def deactivate(self, channel: Channel) -> None:
        """Stop posting to a channel without losing its history."""
        channel.is_active = False
        await self.session.flush()
