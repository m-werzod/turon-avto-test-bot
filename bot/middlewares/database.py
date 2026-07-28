"""Per-update database session."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bot.utils.logging import get_logger

logger = get_logger(__name__)


class DatabaseMiddleware(BaseMiddleware):
    """Opens one session per update and commits it on success.

    Registered as an *outer* middleware so the session exists before any filter
    runs. One session per update means a handler and every service it calls share
    a transaction: either the whole interaction lands, or none of it does. A
    handler that connects a channel and writes an audit row cannot leave the
    first without the second.
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self.session_factory = session_factory
        super().__init__()

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        async with self.session_factory() as session:
            data["session"] = session
            try:
                result = await handler(event, data)
                await session.commit()
                return result
            except Exception:
                await session.rollback()
                raise
