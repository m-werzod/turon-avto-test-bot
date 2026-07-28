"""Shared helpers for handlers."""

from __future__ import annotations

from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

from bot.utils.logging import get_logger

logger = get_logger(__name__)


async def safe_edit(
    callback: CallbackQuery,
    text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
    *,
    parse_mode: str | None = "HTML",
) -> Message | None:
    """Edit the message behind a callback, tolerating a no-op edit.

    Telegram raises "message is not modified" when the new content is identical
    to the old — which happens constantly with a Refresh button whose data has
    not changed. That is a success, not an error, so it is swallowed here rather
    than in every caller.

    Returns:
        The edited message, or ``None`` when nothing changed or the message is
        no longer editable.
    """
    if callback.message is None:
        return None

    try:
        result = await callback.message.edit_text(
            text=text, reply_markup=reply_markup, parse_mode=parse_mode
        )
    except TelegramBadRequest as exc:
        message = str(exc).lower()
        if "message is not modified" in message:
            return None
        if "message to edit not found" in message or "message can't be edited" in message:
            # Too old to edit (48h) or already deleted: send a fresh one so the
            # admin still gets an answer.
            logger.debug("Falling back to a new message: %s", exc)
            return await callback.message.answer(
                text=text, reply_markup=reply_markup, parse_mode=parse_mode
            )
        raise

    return result if isinstance(result, Message) else None


async def answer_callback(callback: CallbackQuery, text: str = "", *, alert: bool = False) -> None:
    """Acknowledge a callback, ignoring an expired query.

    An unanswered callback leaves a spinner stuck on the admin's button, and a
    query older than ~15s can no longer be answered — neither is worth raising.
    """
    try:
        await callback.answer(text=text or None, show_alert=alert)
    except TelegramBadRequest as exc:
        logger.debug("Could not answer callback: %s", exc)
