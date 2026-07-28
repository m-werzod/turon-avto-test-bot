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

    ``owner_id`` is the caller's own Telegram id, and it is what every per-user
    repository scopes on. Each person runs their own channels, schedule and cycle
    over the shared question bank, so a handler that forgets to scope would show
    one user another's data.

    ``is_admin`` no longer gates the panel — everybody gets their own. It marks
    the installation's operator, who alone may refresh the shared question bank,
    read the server logs and take backups. It is decided against the ``ADMIN_IDS``
    environment value, never against the database column: that column is a cached
    convenience for display, and treating it as authoritative would let a database
    write hand somebody those powers.
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
            data.setdefault("owner_id", None)
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
        data["owner_id"] = telegram_user.id
        data["is_new_user"] = is_new
        data["lang"] = translator.normalize(stored.language)
        data["is_admin"] = is_admin

        return await handler(event, data)
