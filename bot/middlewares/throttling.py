"""Simple per-user rate limiting."""

from __future__ import annotations

import time
from collections import defaultdict
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, TelegramObject

from bot.utils.logging import get_logger

logger = get_logger(__name__)


class ThrottlingMiddleware(BaseMiddleware):
    """Drops updates from a user who is sending them too fast.

    Guards against an admin button-mashing "Send now" and firing several posts,
    and against a stranger flooding the bot. In-memory rather than Redis-backed:
    the bot runs as a single process, so an extra service would add a dependency
    and a failure mode for no gain.
    """

    def __init__(self, rate_limit: float = 0.7) -> None:
        """Args:
        rate_limit: Minimum seconds between accepted updates per user.
        """
        self.rate_limit = rate_limit
        self._last_seen: dict[int, float] = defaultdict(float)
        super().__init__()

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user = data.get("event_from_user")
        if user is None:
            return await handler(event, data)

        now = time.monotonic()
        elapsed = now - self._last_seen[user.id]

        if elapsed < self.rate_limit:
            logger.debug("Throttled user %d (%.2fs since last update)", user.id, elapsed)
            if isinstance(event, CallbackQuery):
                # Always answer a callback: an unanswered one leaves a spinner
                # stuck on the admin's button.
                await event.answer()
            return None

        self._last_seen[user.id] = now

        # Bound memory on a bot that many strangers have touched.
        if len(self._last_seen) > 10_000:
            cutoff = now - 60
            self._last_seen = defaultdict(
                float, {uid: ts for uid, ts in self._last_seen.items() if ts > cutoff}
            )

        return await handler(event, data)
