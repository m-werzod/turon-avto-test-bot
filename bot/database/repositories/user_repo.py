"""Bot user storage."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import func, select

from bot.database.models.user import BotUser
from bot.database.repositories.base import BaseRepository


class UserRepository(BaseRepository[BotUser]):
    """Users and their language preference."""

    model = BotUser

    async def get_by_telegram_id(self, telegram_id: int) -> BotUser | None:
        """Look a user up by Telegram id."""
        return await self.session.scalar(select(BotUser).where(BotUser.telegram_id == telegram_id))

    async def touch(
        self,
        *,
        telegram_id: int,
        username: str | None = None,
        first_name: str | None = None,
        last_name: str | None = None,
        is_admin: bool = False,
        default_language: str = "uz",
    ) -> BotUser:
        """Record that a user interacted, creating the row on first contact.

        Profile fields are refreshed on every call so a renamed user does not go
        stale, but ``language`` is left alone — it is the user's own choice and
        must not be reset by an incidental update.

        Returns:
            The stored user.
        """
        user = await self.get_by_telegram_id(telegram_id)
        now = datetime.now(UTC)

        if user is None:
            user = BotUser(
                telegram_id=telegram_id,
                username=username,
                first_name=first_name,
                last_name=last_name,
                language=default_language,
                is_admin=is_admin,
                last_seen_at=now,
            )
            await self.add(user)
            return user

        self.apply_updates(
            user,
            username=username,
            first_name=first_name,
            last_name=last_name,
            is_admin=is_admin,
        )
        user.last_seen_at = now
        await self.session.flush()
        return user

    async def set_language(self, telegram_id: int, language: str) -> BotUser | None:
        """Persist a language choice."""
        user = await self.get_by_telegram_id(telegram_id)
        if user is None:
            return None
        user.language = language
        await self.session.flush()
        return user

    async def get_language(self, telegram_id: int, default: str = "uz") -> str:
        """Read a user's language, or the default when unknown."""
        stmt = select(BotUser.language).where(BotUser.telegram_id == telegram_id)
        return await self.session.scalar(stmt) or default

    async def count_by_language(self) -> dict[str, int]:
        """User counts grouped by interface language."""
        stmt = select(BotUser.language, func.count()).group_by(BotUser.language)
        result = await self.session.execute(stmt)
        return {language: count for language, count in result.all()}  # noqa: C416
