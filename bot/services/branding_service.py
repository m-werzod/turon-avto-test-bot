"""Sending the bot logo alongside its greeting.

The logo goes out with every ``/start``, so uploading the file each time would
be wasteful. Telegram hands back a ``file_id`` for anything it has stored, and
that id can be resent indefinitely at no bandwidth cost — this caches it in the
settings table so the saving survives a restart too.
"""

from __future__ import annotations

from pathlib import Path

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.types import FSInputFile, InlineKeyboardMarkup, Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot.database.repositories import SettingsRepository
from bot.utils.logging import get_logger
from bot.utils.text import CAPTION_LIMIT, truncate

logger = get_logger(__name__)

#: Settings key holding the cached Telegram file id for the logo.
LOGO_FILE_ID_KEY = "brand.logo_file_id"


class BrandingService:
    """Sends the logo with a caption, reusing Telegram's copy where possible."""

    def __init__(self, bot: Bot, logo_path: Path) -> None:
        """Configure the sender.

        Args:
            bot: aiogram bot instance.
            logo_path: Local logo file. A missing file is not an error — the
                caller falls back to a plain text message.
        """
        self.bot = bot
        self.logo_path = logo_path

    async def send_welcome(
        self,
        session: AsyncSession,
        chat_id: int,
        caption: str,
        *,
        reply_markup: InlineKeyboardMarkup | None = None,
    ) -> Message:
        """Send the logo captioned with ``caption``.

        Falls back to a text-only message when no logo is configured or Telegram
        refuses the upload: a greeting without a picture is a much better outcome
        than no greeting at all.

        Args:
            session: Open session, used to read and update the cached file id.
            chat_id: Recipient.
            caption: Message body. Clamped to Telegram's caption limit.
            reply_markup: Optional keyboard.

        Returns:
            The sent message.
        """
        caption = truncate(caption, CAPTION_LIMIT)
        settings_repo = SettingsRepository(session)

        cached = await settings_repo.get_raw(LOGO_FILE_ID_KEY)
        if cached:
            try:
                return await self.bot.send_photo(
                    chat_id=chat_id,
                    photo=cached,
                    caption=caption,
                    parse_mode="HTML",
                    reply_markup=reply_markup,
                )
            except TelegramBadRequest as exc:
                # A cached id goes stale if the bot token changes. Drop it and
                # re-upload rather than failing every greeting from here on.
                logger.warning("Cached logo file_id rejected (%s); re-uploading", exc.message)
                await settings_repo.set_raw(LOGO_FILE_ID_KEY, None)

        if not self.logo_path.exists():
            logger.debug("No logo at %s; sending text-only greeting", self.logo_path)
            return await self.bot.send_message(
                chat_id=chat_id, text=caption, parse_mode="HTML", reply_markup=reply_markup
            )

        try:
            message = await self.bot.send_photo(
                chat_id=chat_id,
                photo=FSInputFile(self.logo_path),
                caption=caption,
                parse_mode="HTML",
                reply_markup=reply_markup,
            )
        except TelegramAPIError as exc:
            logger.warning("Could not send logo (%s); falling back to text", exc)
            return await self.bot.send_message(
                chat_id=chat_id, text=caption, parse_mode="HTML", reply_markup=reply_markup
            )

        if message.photo:
            # Largest rendition last; that is the one worth caching.
            await settings_repo.set_raw(LOGO_FILE_ID_KEY, message.photo[-1].file_id)

        return message
