"""The admin main menu."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot.database.repositories import SettingsRepository
from bot.handlers.helpers import answer_callback, safe_edit
from bot.keyboards import CB, main_menu_keyboard
from bot.locales.i18n import t
from bot.utils.logging import get_logger

logger = get_logger(__name__)

router = Router(name="admin-menu")


@router.message(Command("admin", "panel", "menu"))
async def open_panel(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    owner_id: int,
    lang: str = "uz",
    is_admin: bool = False,
) -> None:
    """Open the admin panel from a command."""
    await state.clear()
    paused = await SettingsRepository(session, owner_id).is_scheduler_paused()
    await message.answer(
        t("menu.title", lang),
        reply_markup=main_menu_keyboard(lang, paused=paused, is_operator=is_admin),
    )


@router.callback_query(F.data == CB.MENU)
async def show_menu(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    owner_id: int,
    lang: str = "uz",
    is_admin: bool = False,
) -> None:
    """Return to the main menu, cancelling any flow in progress.

    Clearing state here is what makes "Back" a reliable escape hatch: an admin
    who opened the channel prompt and changed their mind must not stay stuck
    waiting for input.
    """
    await state.clear()
    paused = await SettingsRepository(session, owner_id).is_scheduler_paused()
    await answer_callback(callback)
    await safe_edit(
        callback,
        t("menu.title", lang),
        reply_markup=main_menu_keyboard(lang, paused=paused, is_operator=is_admin),
    )


@router.callback_query(F.data == CB.NOOP)
async def ignore_noop(callback: CallbackQuery) -> None:
    """Acknowledge decorative buttons so they do not spin forever."""
    await answer_callback(callback)


@router.message(Command("cancel"))
async def cancel_flow(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    owner_id: int,
    lang: str = "uz",
    is_admin: bool = False,
) -> None:
    """Abort whatever multi-step flow is running."""
    await state.clear()
    paused = await SettingsRepository(session, owner_id).is_scheduler_paused()
    await message.answer(
        t("common.cancelled", lang),
        reply_markup=main_menu_keyboard(lang, paused=paused, is_operator=is_admin),
    )
