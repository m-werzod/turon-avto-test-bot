"""User identity and language resolution."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject, User

from bot.database.repositories import UserRepository
from bot.locales.i18n import translator
from bot.utils.logging import get_logger

logger = get_logger(__name__)


class UserContextMiddleware(BaseMiddleware):
    """Records the user and resolves their interface language.

    Puts ``user``, ``lang`` and ``is_admin`` into the handler data so no handler
    has to repeat the lookup.

    ``is_admin`` is decided against the ``ADMIN_IDS`` environment value, never
    against the database column. The column is a cached convenience for display;
    treating it as authoritative would mean a database write could grant somebody
    the admin panel.
    """

    def __init__(self, admin_ids: frozenset[int], default_language: str = "uz") -> None:
        self.admin_ids = admin_ids
        self.default_language = default_language
        super().__init__()

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        telegram_user: User | None = data.get("event_from_user")
        session = data.get("session")

        if telegram_user is None or session is None:
            # Channel posts and similar updates carry no user; nothing to resolve.
            data.setdefault("lang", self.default_language)
            data.setdefault("is_admin", False)
            return await handler(event, data)

        is_admin = telegram_user.id in self.admin_ids
        users = UserRepository(session)

        stored, is_new = await users.touch(
            telegram_id=telegram_user.id,
            username=telegram_user.username,
            first_name=telegram_user.first_name,
            last_name=telegram_user.last_name,
            is_admin=is_admin,
            # Seed a first-time user from their Telegram client language, so the
            # picker opens on the option they most likely want.
            default_language=translator.normalize(telegram_user.language_code),
        )

        data["user"] = stored
        data["is_new_user"] = is_new
        data["lang"] = translator.normalize(stored.language)
        data["is_admin"] = is_admin

        return await handler(event, data)
