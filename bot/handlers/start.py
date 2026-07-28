"""``/start``, ``/help`` and language selection — the public-facing surface."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot.database.models.user import BotUser
from bot.database.repositories import SettingsRepository, UserRepository
from bot.handlers.helpers import answer_callback, safe_edit
from bot.keyboards import CB, language_keyboard, main_menu_keyboard
from bot.locales.i18n import LANGUAGE_LABELS, t, translator
from bot.utils.logging import get_logger
from bot.utils.text import escape_html

logger = get_logger(__name__)

router = Router(name="start")


@router.message(CommandStart())
async def handle_start(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    user: BotUser | None = None,
    lang: str = "uz",
    is_admin: bool = False,
) -> None:
    """Greet the user, offering the language picker on first contact.

    A returning user goes straight to the greeting; only someone the bot has not
    seen before is asked to choose, so the picker never becomes a toll gate on
    every ``/start``.
    """
    # Any half-finished flow is abandoned: /start is how a stuck admin recovers.
    await state.clear()

    if user is None or user.created_at == user.updated_at:
        await message.answer(t("start.choose_language", lang), reply_markup=language_keyboard())
        return

    await _send_greeting(message, session, lang, is_admin)


@router.message(Command("help"))
async def handle_help(
    message: Message,
    session: AsyncSession,
    lang: str = "uz",
    is_admin: bool = False,
) -> None:
    """Same greeting as ``/start``, without resetting the language."""
    await _send_greeting(message, session, lang, is_admin)


@router.message(Command("language"))
async def handle_language_command(message: Message, lang: str = "uz") -> None:
    """Reopen the language picker on demand."""
    await message.answer(t("start.choose_language", lang), reply_markup=language_keyboard())


@router.callback_query(F.data.startswith(f"{CB.SET_UI_LANG}:"))
async def handle_language_choice(
    callback: CallbackQuery,
    session: AsyncSession,
    is_admin: bool = False,
) -> None:
    """Persist the chosen interface language and greet in it."""
    code = (callback.data or "").rsplit(":", 1)[-1]
    language = translator.normalize(code)

    if callback.from_user is not None:
        await UserRepository(session).set_language(callback.from_user.id, language)

    await answer_callback(
        callback, t("start.language_saved", language, language=LANGUAGE_LABELS[language])
    )

    name = escape_html(callback.from_user.full_name if callback.from_user else "")
    if is_admin:
        paused = await SettingsRepository(session).is_scheduler_paused()
        await safe_edit(
            callback,
            t("start.welcome_admin", language, name=name),
            reply_markup=main_menu_keyboard(language, paused=paused),
        )
    else:
        await safe_edit(callback, t("start.welcome_user", language, name=name))

    logger.info(
        "User %s selected language %s",
        getattr(callback.from_user, "id", None),
        language,
    )


async def _send_greeting(
    message: Message, session: AsyncSession, language: str, is_admin: bool
) -> None:
    """Send the greeting appropriate to the caller's role."""
    name = escape_html(message.from_user.full_name if message.from_user else "")

    if is_admin:
        paused = await SettingsRepository(session).is_scheduler_paused()
        await message.answer(
            t("start.welcome_admin", language, name=name),
            reply_markup=main_menu_keyboard(language, paused=paused),
        )
    else:
        await message.answer(t("start.welcome_user", language, name=name))
