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

    # A callback attached to a message older than 48 hours arrives as an
    # InaccessibleMessage, which is not a Message subclass and has no edit_text.
    # Calling it would raise AttributeError, so answer with a fresh message
    # instead — the admin still gets a reply.
    if not isinstance(callback.message, Message):
        logger.debug("Callback message is inaccessible; sending a new message instead")
        return await callback.message.answer(
            text=text, reply_markup=reply_markup, parse_mode=parse_mode
        )

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


async def safe_delete(callback: CallbackQuery) -> None:
    """Delete the message behind a callback, tolerating every ordinary refusal.

    Deletion is best-effort by nature: the message may already be gone, be older
    than the 48-hour window, or sit in a chat where the bot lacks the right. None
    of those should surface to the user, who is about to receive a replacement
    message regardless.
    """
    if callback.message is None or not isinstance(callback.message, Message):
        return

    try:
        await callback.message.delete()
    except TelegramBadRequest as exc:
        logger.debug("Could not delete message: %s", exc)


async def answer_callback(callback: CallbackQuery, text: str = "", *, alert: bool = False) -> None:
    """Acknowledge a callback, ignoring an expired query.

    An unanswered callback leaves a spinner stuck on the admin's button, and a
    query older than ~15s can no longer be answered — neither is worth raising.
    """
    try:
        await callback.answer(text=text or None, show_alert=alert)
    except TelegramBadRequest as exc:
        logger.debug("Could not answer callback: %s", exc)
