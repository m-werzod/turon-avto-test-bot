"""The "Update tests" flow: import questions from a file."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot.database.models.setting import SettingKey
from bot.database.repositories import QuestionRepository, SettingsRepository
from bot.handlers.helpers import answer_callback, safe_edit
from bot.keyboards import CB, back_keyboard, import_keyboard
from bot.locales.i18n import t
from bot.services.import_service import ImportReport, ImportService
from bot.sources.base import QuestionSource, SourceError
from bot.sources.registry import (
    DATA_DIR,
    SUPPORTED_EXTENSIONS,
    WEB_SOURCES,
    build_source,
    build_web_source,
    discover_data_files,
)
from bot.states import ImportStates
from bot.utils.logging import get_logger
from bot.utils.text import escape_html

logger = get_logger(__name__)

router = Router(name="admin-import")

#: Refuse uploads above this size; the Bot API caps downloads at 20 MB anyway.
_MAX_UPLOAD_BYTES = 20 * 1024 * 1024


@router.callback_query(F.data == CB.IMPORT)
async def show_import(
    callback: CallbackQuery, state: FSMContext, session: AsyncSession, lang: str = "uz"
) -> None:
    """Show the import menu with any files found on disk."""
    await state.clear()
    await answer_callback(callback)

    total = await QuestionRepository(session).count_active()
    last_import = await SettingsRepository(session).get_raw(SettingKey.LAST_IMPORT_AT)
    last_label = t("import.never", lang)
    if last_import:
        try:
            last_label = datetime.fromisoformat(last_import).strftime("%d.%m.%Y %H:%M")
        except ValueError:
            last_label = last_import

    files = discover_data_files()
    body = t("import.intro", lang, total=total, last=last_label)

    await safe_edit(
        callback,
        f"{t('import.title', lang)}\n\n{body}",
        reply_markup=import_keyboard(
            lang,
            [path.name for path in files],
            [(key, label) for key, (label, _) in WEB_SOURCES.items()],
        ),
    )


@router.callback_query(F.data.startswith(f"{CB.IMPORT_FILE}:"))
async def import_from_disk(
    callback: CallbackQuery,
    session: AsyncSession,
    import_service: ImportService,
    lang: str = "uz",
) -> None:
    """Import a file already present in ``data/``."""
    raw_index = (callback.data or "").rsplit(":", 1)[-1]
    files = discover_data_files()

    # Re-list rather than trusting the index blindly: the directory may have
    # changed since the keyboard was drawn.
    if not raw_index.isdigit() or int(raw_index) >= len(files):
        await answer_callback(callback, t("import.no_files", lang), alert=True)
        return

    await answer_callback(callback)
    chosen = files[int(raw_index)]
    await _run_import(callback, session, import_service, lambda: build_source(chosen), lang)


@router.callback_query(F.data.startswith(f"{CB.IMPORT_WEB}:"))
async def import_from_website(
    callback: CallbackQuery,
    session: AsyncSession,
    import_service: ImportService,
    lang: str = "uz",
) -> None:
    """Import from the chosen website."""
    await answer_callback(callback)

    key = (callback.data or "").rsplit(":", 1)[-1]
    content_language = await SettingsRepository(session).content_language()

    await _run_import(
        callback,
        session,
        import_service,
        lambda: build_web_source(key, language=content_language),
        lang,
        running_key="import.running_web",
    )


@router.callback_query(F.data == CB.IMPORT_UPLOAD)
async def prompt_for_upload(callback: CallbackQuery, state: FSMContext, lang: str = "uz") -> None:
    """Ask the admin to send a question file."""
    await state.set_state(ImportStates.waiting_for_file)
    await answer_callback(callback)
    await safe_edit(
        callback, t("import.upload_prompt", lang), reply_markup=back_keyboard(lang, CB.IMPORT)
    )


@router.message(ImportStates.waiting_for_file, F.document)
async def receive_upload(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    bot: Bot,
    import_service: ImportService,
    lang: str = "uz",
) -> None:
    """Download an uploaded file into ``data/`` and import it."""
    document = message.document
    if document is None:
        return

    suffix = Path(document.file_name or "").suffix.lower()
    if suffix not in SUPPORTED_EXTENSIONS:
        await message.answer(
            t(
                "import.invalid_file",
                lang,
                reason=f"{escape_html(suffix or '?')} — supported: "
                f"{', '.join(SUPPORTED_EXTENSIONS)}",
            ),
            parse_mode="HTML",
        )
        return

    if (document.file_size or 0) > _MAX_UPLOAD_BYTES:
        await message.answer(
            t(
                "import.invalid_file",
                lang,
                reason=f"{(document.file_size or 0) / 1_048_576:.1f} MB > 20 MB",
            ),
            parse_mode="HTML",
        )
        return

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    # Keep the admin's filename but stamp it, so re-uploading a corrected file
    # does not overwrite the copy an earlier import already recorded.
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    destination = DATA_DIR / f"{Path(document.file_name or 'questions').stem}_{stamp}{suffix}"

    status = await message.answer(t("import.running", lang))

    try:
        await bot.download(document, destination=destination)
    except Exception as exc:
        logger.exception("Failed to download uploaded file")
        await status.edit_text(
            t("import.invalid_file", lang, reason=escape_html(str(exc)[:200])),
            parse_mode="HTML",
        )
        return

    await state.clear()
    await _run_import(status, session, import_service, lambda: build_source(destination), lang)


@router.message(ImportStates.waiting_for_file)
async def reject_non_document(message: Message, lang: str = "uz") -> None:
    """Remind the admin that this step expects a file."""
    await message.answer(t("import.upload_prompt", lang), parse_mode="HTML")


async def _run_import(
    target: CallbackQuery | Message,
    session: AsyncSession,
    import_service: ImportService,
    source_factory: Callable[[], QuestionSource],
    language: str,
    *,
    running_key: str = "import.running",
) -> None:
    """Execute an import and report the outcome.

    Takes a factory rather than a path so a file reader and the website scraper
    share one reporting path; building the source is inside the try block because
    an unsupported extension is itself an error worth showing the admin.

    Args:
        target: Callback whose message is edited, or a message to edit directly.
        session: Open session.
        import_service: Importer.
        source_factory: Builds the source to read.
        language: Admin's language.
        running_key: Locale key for the "working" message.
    """

    async def render(text: str, markup: object | None = None) -> None:
        if isinstance(target, CallbackQuery):
            await safe_edit(target, text, reply_markup=markup)  # type: ignore[arg-type]
        else:
            await target.edit_text(text, reply_markup=markup, parse_mode="HTML")  # type: ignore[arg-type]

    await render(t(running_key, language))

    try:
        source = source_factory()
        report: ImportReport = await import_service.import_source(session, source)
    except SourceError as exc:
        logger.warning("Import failed: %s", exc)
        await render(
            t("import.failed", language, reason=escape_html(str(exc)[:400])),
            back_keyboard(language, CB.IMPORT),
        )
        return
    except Exception as exc:
        logger.exception("Unexpected import failure")
        await render(
            t("import.failed", language, reason=escape_html(str(exc)[:400])),
            back_keyboard(language, CB.IMPORT),
        )
        return

    text = t(
        "import.finished",
        language,
        created=report.result.created,
        updated=report.result.updated,
        unchanged=report.result.unchanged,
        skipped=report.result.skipped,
        images=report.images_downloaded,
        total=report.total_in_bank,
    )

    if report.validation_errors:
        samples = "\n".join(
            f"• {escape_html(error[:120])}" for error in report.validation_errors[:5]
        )
        text += "\n\n" + t(
            "import.validation_errors",
            language,
            count=len(report.validation_errors),
            samples=samples,
        )

    await render(text, back_keyboard(language, CB.IMPORT))
