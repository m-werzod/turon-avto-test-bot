"""Global error handler.

The last line of defence. Any exception escaping a handler lands here, gets
logged and reported, and the update is marked handled — so one bad update can
never stop the dispatcher.
"""

from __future__ import annotations

from aiogram import Router
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.types import CallbackQuery, ErrorEvent, Message

from bot.locales.i18n import t
from bot.services.notify_service import NotifyService
from bot.utils.logging import get_logger

logger = get_logger(__name__)

router = Router(name="errors")


@router.error()
async def handle_error(event: ErrorEvent, notify: NotifyService | None = None) -> bool:
    """Log, tell the user something went wrong, and alert the admins.

    Args:
        event: The failing update and its exception.
        notify: Alert service. Optional so the dispatcher still works without it.

    Returns:
        Always ``True``: the error is considered handled, which keeps polling
        alive.
    """
    exception = event.exception
    update = event.update

    # A user blocking the bot is routine, not an incident worth alerting on.
    if isinstance(exception, TelegramForbiddenError):
        logger.info("Bot was blocked or removed: %s", exception)
        return True

    if isinstance(exception, TelegramRetryAfter):
        logger.warning("Flood control: retry after %ds", exception.retry_after)
        return True

    if (
        isinstance(exception, TelegramBadRequest)
        and "message is not modified" in str(exception).lower()
    ):
        # A refresh button pressed twice; harmless.
        return True

    logger.exception(
        "Unhandled exception while processing update %s",
        getattr(update, "update_id", "?"),
        exc_info=exception,
    )

    message = getattr(update, "message", None)
    callback = getattr(update, "callback_query", None)
    language = "uz"

    try:
        if isinstance(callback, CallbackQuery):
            await callback.answer(t("common.error", language), show_alert=True)
        elif isinstance(message, Message):
            await message.answer(t("common.error", language))
    except Exception:  # noqa: BLE001 - telling the user must not fail the handler
        logger.debug("Could not deliver the error notice to the user")

    if notify is not None:
        where = f"update:{getattr(update, 'update_id', '?')}"
        await notify.notify_error(where, exception)

    return True
