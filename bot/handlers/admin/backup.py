"""Database backup export."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.types import BufferedInputFile, CallbackQuery
from sqlalchemy.ext.asyncio import AsyncSession

from bot.handlers.helpers import answer_callback, safe_edit
from bot.keyboards import CB, back_keyboard, backup_keyboard
from bot.locales.i18n import t
from bot.services.backup_service import BackupService
from bot.utils.logging import get_logger
from bot.utils.text import escape_html

logger = get_logger(__name__)

router = Router(name="admin-backup")


@router.callback_query(F.data == CB.BACKUP)
async def show_backup(callback: CallbackQuery, lang: str = "uz") -> None:
    """Explain what a backup contains."""
    await answer_callback(callback)
    await safe_edit(
        callback,
        f"{t('backup.title', lang)}\n\n{t('backup.intro', lang)}",
        reply_markup=backup_keyboard(lang),
    )


@router.callback_query(F.data == CB.BACKUP_CREATE)
async def create_backup(
    callback: CallbackQuery,
    session: AsyncSession,
    backup_service: BackupService,
    lang: str = "uz",
) -> None:
    """Build an archive and send it to the admin."""
    await answer_callback(callback)
    await safe_edit(callback, t("backup.creating", lang))

    try:
        result = await backup_service.create(session)
    except Exception as exc:
        logger.exception("Backup failed")
        await safe_edit(
            callback,
            t("backup.failed", lang, reason=escape_html(str(exc)[:300])),
            reply_markup=back_keyboard(lang),
        )
        return

    summary = t(
        "backup.ready",
        lang,
        size=result.human_size,
        questions=result.row_counts.get("questions", 0),
        deliveries=result.row_counts.get("deliveries", 0),
    )

    if not result.fits_telegram:
        # Still a success — the archive exists on disk, it just cannot be sent.
        await safe_edit(
            callback,
            t("backup.too_large", lang, size=result.human_size, path=str(result.path)),
            reply_markup=back_keyboard(lang),
        )
        return

    if callback.message is None:
        return

    try:
        payload = result.path.read_bytes()
        await callback.message.answer_document(
            document=BufferedInputFile(payload, filename=result.path.name),
            caption=summary,
            parse_mode="HTML",
        )
        await safe_edit(callback, summary, reply_markup=back_keyboard(lang))
    except Exception as exc:
        logger.exception("Could not send backup archive")
        await safe_edit(
            callback,
            t("backup.too_large", lang, size=result.human_size, path=str(result.path)),
            reply_markup=back_keyboard(lang),
        )
        logger.info("Backup remains available at %s (%s)", result.path, exc)
