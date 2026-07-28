"""Settings panel."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.types import CallbackQuery
from sqlalchemy.ext.asyncio import AsyncSession

from bot.database.repositories import SettingsRepository, UserRepository
from bot.handlers.helpers import answer_callback, safe_edit
from bot.keyboards import CB, back_keyboard, language_keyboard, settings_keyboard
from bot.locales.i18n import LANGUAGE_LABELS, t, translator
from bot.utils.logging import get_logger

logger = get_logger(__name__)

router = Router(name="admin-settings")


@router.callback_query(F.data == CB.SETTINGS)
async def show_settings(
    callback: CallbackQuery,
    session: AsyncSession,
    owner_id: int,
    lang: str = "uz",
    timezone: object | None = None,
    send_as_document: bool = False,
) -> None:
    """Show current settings."""
    await answer_callback(callback)
    settings_repo = SettingsRepository(session, owner_id)

    content_language = await settings_repo.content_language()
    skip_weekends = await settings_repo.skip_weekends()

    body = t(
        "settings.body",
        lang,
        ui_language=LANGUAGE_LABELS.get(lang, lang),
        content_language=LANGUAGE_LABELS.get(content_language, content_language),
        weekends=t("settings.weekends_skip" if skip_weekends else "settings.weekends_send", lang),
        image_mode=t(
            "settings.image_document" if send_as_document else "settings.image_photo", lang
        ),
        timezone=str(timezone or "Asia/Tashkent"),
    )

    await safe_edit(
        callback,
        f"{t('settings.title', lang)}\n\n{body}",
        reply_markup=settings_keyboard(lang),
    )


@router.callback_query(F.data == CB.SETTINGS_UI_LANG)
async def choose_ui_language(callback: CallbackQuery, lang: str = "uz") -> None:
    """Open the interface language picker."""
    await answer_callback(callback)
    await safe_edit(callback, t("start.choose_language", lang), reply_markup=language_keyboard())


@router.callback_query(F.data == CB.SETTINGS_CONTENT_LANG)
async def choose_content_language(callback: CallbackQuery, lang: str = "uz") -> None:
    """Open the picker for which language of questions to publish."""
    await answer_callback(callback)
    await safe_edit(
        callback,
        t("settings.content_language_prompt", lang),
        reply_markup=language_keyboard(CB.SET_CONTENT_LANG),
    )


@router.callback_query(F.data.startswith(f"{CB.SET_CONTENT_LANG}:"))
async def set_content_language(
    callback: CallbackQuery, session: AsyncSession, owner_id: int, lang: str = "uz"
) -> None:
    """Persist which language of questions the bot publishes.

    Separate from the interface language on purpose: an admin reading a Russian
    panel may well be running an Uzbek-language channel.
    """
    code = (callback.data or "").rsplit(":", 1)[-1]
    language = translator.normalize(code)

    await SettingsRepository(session, owner_id).set_content_language(language)
    await answer_callback(
        callback,
        t("settings.content_language_saved", lang, language=LANGUAGE_LABELS[language]),
    )
    await safe_edit(
        callback,
        t("settings.content_language_saved", lang, language=LANGUAGE_LABELS[language]),
        reply_markup=back_keyboard(lang, CB.SETTINGS),
    )
    logger.info("Content language set to %s", language)


@router.callback_query(F.data == CB.SETTINGS_UI_LANG + ":noop")
async def noop_language(callback: CallbackQuery) -> None:
    """Acknowledge a stale language button."""
    await answer_callback(callback)


async def _refresh_user_language(
    session: AsyncSession, owner_id: int, telegram_id: int, language: str
) -> None:
    """Persist an interface language change."""
    await UserRepository(session).set_language(telegram_id, language)
