"""Admin-only access control."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject

from bot.locales.i18n import t
from bot.utils.logging import get_logger

logger = get_logger(__name__)


class AdminOnlyMiddleware(BaseMiddleware):
    """Blocks every non-admin from the routers it is attached to.

    Attached to the admin router rather than checked inside each handler: a
    per-handler check is one forgotten line away from exposing the panel, whereas
    a router-level gate is impossible to skip for a new handler added later.
    """

    def __init__(self, admin_ids: frozenset[int]) -> None:
        self.admin_ids = admin_ids
        super().__init__()

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user = data.get("event_from_user")
        if user is not None and user.id in self.admin_ids:
            return await handler(event, data)

        language = data.get("lang", "uz")
        logger.warning(
            "Denied admin access to user %s (@%s)",
            getattr(user, "id", "unknown"),
            getattr(user, "username", None),
            extra={"user_id": getattr(user, "id", None)},
        )

        # Answer rather than ignore, so a legitimate admin who mistyped their id
        # in .env gets a clear signal instead of silence.
        if isinstance(event, CallbackQuery):
            await event.answer(t("common.not_admin", language), show_alert=True)
        elif isinstance(event, Message):
            await event.answer(t("common.not_admin", language))

        return None
