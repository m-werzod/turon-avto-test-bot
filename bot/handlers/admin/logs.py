"""In-Telegram log viewer."""

from __future__ import annotations

from datetime import UTC
from pathlib import Path

from aiogram import F, Router
from aiogram.types import BufferedInputFile, CallbackQuery
from sqlalchemy.ext.asyncio import AsyncSession

from bot.database.repositories import EventRepository
from bot.handlers.helpers import answer_callback, safe_edit
from bot.keyboards import CB, back_keyboard, logs_keyboard
from bot.locales.i18n import t
from bot.utils.logging import get_logger
from bot.utils.text import MESSAGE_LIMIT, escape_html, truncate

logger = get_logger(__name__)

router = Router(name="admin-logs")

#: Entries shown in one screen.
_PAGE_SIZE = 15

#: Tail of the log file sent as a download, in bytes.
_LOG_TAIL_BYTES = 256 * 1024

_LEVEL_ICONS = {
    "DEBUG": "🔹",
    "INFO": "ℹ️",
    "WARNING": "⚠️",
    "ERROR": "❌",
    "CRITICAL": "🚨",
}


@router.callback_query(F.data.in_({CB.LOGS, CB.LOGS_ALL}))
async def show_logs(
    callback: CallbackQuery, session: AsyncSession, lang: str = "uz", **kwargs: object
) -> None:
    """Show recent events of every level."""
    await answer_callback(callback)
    await _render_logs(callback, session, lang, level=None, **kwargs)


@router.callback_query(F.data == CB.LOGS_ERRORS)
async def show_error_logs(
    callback: CallbackQuery, session: AsyncSession, lang: str = "uz", **kwargs: object
) -> None:
    """Show only errors."""
    await answer_callback(callback)
    await _render_logs(callback, session, lang, level="ERROR", **kwargs)


@router.callback_query(F.data == CB.LOGS_DOWNLOAD)
async def download_logs(
    callback: CallbackQuery, lang: str = "uz", log_dir: Path | None = None
) -> None:
    """Send the tail of the log file as a document.

    Only the tail: a rotated ``app.log`` can approach 10 MB, and an admin
    diagnosing a problem wants the most recent lines, not a slow full upload.
    """
    await answer_callback(callback)

    log_path = (log_dir or Path("logs")) / "app.log"
    if not log_path.exists():
        await safe_edit(
            callback, t("logs.file_missing", lang), reply_markup=back_keyboard(lang, CB.LOGS)
        )
        return

    try:
        size = log_path.stat().st_size
        with log_path.open("rb") as handle:
            if size > _LOG_TAIL_BYTES:
                handle.seek(size - _LOG_TAIL_BYTES)
                handle.readline()  # discard the partial first line
            payload = handle.read()

        if callback.message is not None:
            await callback.message.answer_document(
                document=BufferedInputFile(payload, filename="app.log"),
                caption=f"{len(payload) / 1024:.0f} KB",
            )
    except OSError as exc:
        logger.warning("Could not read log file: %s", exc)
        await safe_edit(
            callback, t("logs.file_missing", lang), reply_markup=back_keyboard(lang, CB.LOGS)
        )


async def _render_logs(
    callback: CallbackQuery,
    session: AsyncSession,
    language: str,
    *,
    level: str | None,
    **kwargs: object,
) -> None:
    """Draw a page of events."""
    timezone = kwargs.get("timezone")
    entries = await EventRepository(session).recent(limit=_PAGE_SIZE, level=level)

    if not entries:
        body = t("logs.empty", language)
    else:
        lines = []
        for entry in entries:
            moment = entry.created_at
            if moment.tzinfo is None:
                moment = moment.replace(tzinfo=UTC)
            if timezone is not None:
                moment = moment.astimezone(timezone)  # type: ignore[arg-type]

            lines.append(
                t(
                    "logs.entry",
                    language,
                    time=moment.strftime("%d.%m %H:%M"),
                    icon=_LEVEL_ICONS.get(entry.level, "•"),
                    message=escape_html(entry.message),
                )
            )
        body = "\n".join(lines)

    header = t(
        "logs.showing",
        language,
        count=len(entries),
        filter=t("logs.filter_errors" if level else "logs.filter_all", language),
    )

    text = truncate(f"{t('logs.title', language)}\n\n{header}\n\n{body}", MESSAGE_LIMIT)
    await safe_edit(callback, text, reply_markup=logs_keyboard(language))
