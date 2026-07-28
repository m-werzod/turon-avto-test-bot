"""Operator-only access control."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject

from bot.locales.i18n import t
from bot.utils.logging import get_logger

logger = get_logger(__name__)


class OperatorOnlyMiddleware(BaseMiddleware):
    """Blocks everyone but the installation's operators.

    The panel itself is open — every user runs their own channels and schedule.
    This gates only what acts installation-wide: refreshing the shared question
    bank, reading server logs, taking backups.

    Attached to a router rather than checked inside each handler: a per-handler
    check is one forgotten line away from exposing the feature, whereas a
    router-level gate is impossible to skip for a handler added later.
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
        logger.info(
            "Denied operator-only feature to user %s (@%s)",
            getattr(user, "id", "unknown"),
            getattr(user, "username", None),
            extra={"user_id": getattr(user, "id", None)},
        )

        # Answer rather than ignore, so an operator who mistyped their id in
        # .env gets a clear signal instead of silence.
        if isinstance(event, CallbackQuery):
            await event.answer(t("common.not_operator", language), show_alert=True)
        elif isinstance(event, Message):
            await event.answer(t("common.not_operator", language))

        return None
