"""Request logging for incoming updates."""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject

from bot.utils.logging import get_logger

logger = get_logger(__name__)

#: Longer than this and a handler is worth investigating.
_SLOW_HANDLER_SECONDS = 3.0


class LoggingMiddleware(BaseMiddleware):
    """Logs what each update was and how long handling it took."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user = data.get("event_from_user")
        user_id = getattr(user, "id", None)
        description = self._describe(event)

        started = time.monotonic()
        try:
            return await handler(event, data)
        finally:
            elapsed = time.monotonic() - started
            level = logger.warning if elapsed > _SLOW_HANDLER_SECONDS else logger.info
            level(
                "%s from %s handled in %.2fs",
                description,
                user_id,
                elapsed,
                extra={"user_id": user_id, "update": description, "seconds": round(elapsed, 3)},
            )

    @staticmethod
    def _describe(event: TelegramObject) -> str:
        """Summarise an update without logging full user content."""
        if isinstance(event, Message):
            if event.text:
                # Commands are safe and useful; other text is only counted, so
                # the log never becomes a transcript of private messages.
                if event.text.startswith("/"):
                    return f"command {event.text.split()[0]}"
                return f"message ({len(event.text)} chars)"
            if event.document:
                return f"document {event.document.file_name}"
            if event.photo:
                return "photo"
            return "message"
        if isinstance(event, CallbackQuery):
            return f"callback {event.data}"
        return type(event).__name__
