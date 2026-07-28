"""Typed access to the runtime settings table."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.database.models.setting import Setting, SettingKey
from bot.database.repositories.base import BaseRepository

_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})

#: Upper bound for one batch. Telegram rate-limits a channel at roughly 20
#: messages a minute and an illustrated question costs two, so ten is the most
#: that reliably lands back-to-back.
MAX_QUESTIONS_PER_SEND = 10


class SettingsRepository(BaseRepository[Setting]):
    """Key/value settings with typed getters and setters.

    Values are stored as text and coerced on read. Callers never touch raw rows,
    so a malformed value cannot propagate past this boundary.

    Settings are read in three layers: the owner's own row, then the
    installation-wide row, then the declared default. That is what lets one
    operator pause their schedule without touching anybody else's, while a value
    the installation sets — when the question bank was last refreshed — is
    answered once for everyone.

    Unlike the other per-user repositories this one does *not* extend
    :class:`OwnedRepository`, because here a row with no owner is a legitimate
    global value rather than a scoping bug.
    """

    model = Setting

    def __init__(self, session: AsyncSession, owner_id: int | None = None) -> None:
        """Bind the repository, optionally to one owner.

        Args:
            session: Open session.
            owner_id: Whose settings to read and write. ``None`` operates on the
                installation-wide layer — correct for the importer and for
                start-up, wrong for anything reached from a user's panel.
        """
        super().__init__(session)
        self.owner_id = owner_id

    async def _row(self, key: str, *, owner_id: int | None) -> Setting | None:
        """Fetch one settings row at a specific layer."""
        stmt = select(Setting).where(Setting.key == key)
        stmt = (
            stmt.where(Setting.owner_id.is_(None))
            if owner_id is None
            else stmt.where(Setting.owner_id == owner_id)
        )
        return await self.session.scalar(stmt)

    async def get_raw(self, key: str) -> str | None:
        """Read a value: the owner's, else the global one, else the default."""
        if self.owner_id is not None:
            mine = await self._row(key, owner_id=self.owner_id)
            if mine is not None and mine.value is not None:
                return mine.value

        shared = await self._row(key, owner_id=None)
        if shared is not None and shared.value is not None:
            return shared.value

        return SettingKey.DEFAULTS.get(key)

    async def set_raw(self, key: str, value: str | None) -> None:
        """Write a value at this repository's layer, creating the row if needed."""
        setting = await self._row(key, owner_id=self.owner_id)
        if setting is None:
            self.session.add(Setting(key=key, value=value, owner_id=self.owner_id))
        else:
            setting.value = value
        await self.session.flush()

    async def get_bool(self, key: str, default: bool = False) -> bool:
        """Read a boolean setting."""
        raw = await self.get_raw(key)
        if raw is None:
            return default
        return raw.strip().lower() in _TRUE_VALUES

    async def set_bool(self, key: str, value: bool) -> None:
        """Write a boolean setting."""
        await self.set_raw(key, "true" if value else "false")

    async def get_int(self, key: str, default: int = 0) -> int:
        """Read an integer setting, falling back on malformed input."""
        raw = await self.get_raw(key)
        if raw is None:
            return default
        try:
            return int(raw)
        except ValueError:
            return default

    async def set_int(self, key: str, value: int) -> None:
        """Write an integer setting."""
        await self.set_raw(key, str(value))

    async def get_str(self, key: str, default: str = "") -> str:
        """Read a string setting."""
        raw = await self.get_raw(key)
        return raw if raw is not None else default

    # --- Named accessors for the settings the app actually branches on --------

    async def is_scheduler_paused(self) -> bool:
        """Whether automatic posting is currently suspended."""
        return await self.get_bool(SettingKey.SCHEDULER_PAUSED, default=False)

    async def set_scheduler_paused(self, paused: bool) -> None:
        """Suspend or resume automatic posting."""
        await self.set_bool(SettingKey.SCHEDULER_PAUSED, paused)

    async def questions_per_send(self) -> int:
        """How many questions go out at each scheduled time.

        Clamped on read as well as on write: the value reaches the poll loop
        directly, and a hand-edited row must not be able to fire off hundreds of
        posts at once.
        """
        value = await self.get_int(SettingKey.QUESTIONS_PER_SEND, default=1)
        return max(1, min(value, MAX_QUESTIONS_PER_SEND))

    async def set_questions_per_send(self, count: int) -> None:
        """Set how many questions go out at each scheduled time."""
        await self.set_int(
            SettingKey.QUESTIONS_PER_SEND, max(1, min(count, MAX_QUESTIONS_PER_SEND))
        )

    async def skip_weekends(self) -> bool:
        """Whether Saturday and Sunday posts are suppressed."""
        return await self.get_bool(SettingKey.SKIP_WEEKENDS, default=False)

    async def set_skip_weekends(self, skip: bool) -> None:
        """Enable or disable weekend suppression."""
        await self.set_bool(SettingKey.SKIP_WEEKENDS, skip)

    async def content_language(self) -> str:
        """Language of the questions to publish."""
        return await self.get_str(SettingKey.CONTENT_LANGUAGE, default="uz")

    async def set_content_language(self, language: str) -> None:
        """Choose which language of questions to publish."""
        await self.set_raw(SettingKey.CONTENT_LANGUAGE, language)

    async def all_as_dict(self) -> dict[str, str | None]:
        """Every stored setting, for backups and the settings panel."""
        stmt = select(Setting).order_by(Setting.key)
        if self.owner_id is not None:
            stmt = stmt.where((Setting.owner_id == self.owner_id) | Setting.owner_id.is_(None))
        else:
            stmt = stmt.where(Setting.owner_id.is_(None))
        result = await self.session.scalars(stmt)
        stored = {setting.key: setting.value for setting in result}
        return {**SettingKey.DEFAULTS, **stored}
