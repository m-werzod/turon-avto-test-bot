"""Connecting, listing and removing channels."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot.database.models.event_log import EventType
from bot.database.repositories import (
    ChannelAlreadyConnectedError,
    ChannelRepository,
    EventRepository,
)
from bot.handlers.helpers import answer_callback, safe_edit
from bot.keyboards import CB, back_keyboard, channels_keyboard
from bot.locales.i18n import t
from bot.services.channel_service import ChannelCheckError, ChannelService
from bot.states import ChannelStates
from bot.utils.logging import get_logger
from bot.utils.text import escape_html

logger = get_logger(__name__)

router = Router(name="admin-channels")


@router.callback_query(F.data == CB.CHANNELS)
async def show_channels(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    owner_id: int,
    lang: str = "uz",
) -> None:
    """List connected channels."""
    await state.clear()
    await answer_callback(callback)
    await _render_channel_list(callback, session, owner_id, lang)


@router.callback_query(F.data == CB.CHANNEL_ADD)
async def prompt_for_channel(callback: CallbackQuery, state: FSMContext, lang: str = "uz") -> None:
    """Ask the admin for a channel username."""
    await state.set_state(ChannelStates.waiting_for_identifier)
    await answer_callback(callback)
    await safe_edit(
        callback, t("channel.prompt", lang), reply_markup=back_keyboard(lang, CB.CHANNELS)
    )


@router.message(ChannelStates.waiting_for_identifier, F.text)
async def receive_channel(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    owner_id: int,
    channel_service: ChannelService,
    lang: str = "uz",
) -> None:
    """Verify and store the channel the admin named.

    Verification happens before anything is written, so a channel that cannot
    actually be posted to never enters the database and never silently swallows a
    scheduled slot.
    """
    raw = (message.text or "").strip()
    status = await message.answer(t("channel.checking", lang))

    try:
        info = await channel_service.verify_localized(raw, lang)
    except ChannelCheckError as exc:
        await status.edit_text(
            exc.localized(lang), reply_markup=back_keyboard(lang, CB.CHANNELS), parse_mode="HTML"
        )
        logger.info("Channel check failed for %r: %s", raw, exc.reason_key)
        return
    except Exception as exc:
        logger.exception("Unexpected error verifying channel %r", raw)
        await status.edit_text(
            t("channel.invalid", lang, reason=escape_html(str(exc)[:200])),
            reply_markup=back_keyboard(lang, CB.CHANNELS),
            parse_mode="HTML",
        )
        return

    channels = ChannelRepository(session, owner_id)
    try:
        channel, created = await channels.upsert(
            chat_id=info.chat_id,
            username=info.username,
            title=info.title,
            added_by=message.from_user.id if message.from_user else None,
        )
    except ChannelAlreadyConnectedError:
        # Somebody else already drives this chat. Say so plainly: the alternative
        # is a stack trace for what is an ordinary situation once the bot has
        # more than one user.
        await state.clear()
        await status.edit_text(
            t("channel.already_owned", lang, title=escape_html(info.title)),
            reply_markup=back_keyboard(lang, CB.CHANNELS),
            parse_mode="HTML",
        )
        logger.info(
            "User %d tried to connect chat %d, already owned by another user",
            owner_id,
            info.chat_id,
        )
        return

    await EventRepository(session).record(
        EventType.CHANNEL_CONNECTED,
        f"Channel {'connected' if created else 'reconnected'}: {channel.display_name}",
        payload={"chat_id": info.chat_id, "channel": channel.display_name},
    )

    await state.clear()
    await status.edit_text(
        t(
            "channel.connected",
            lang,
            title=escape_html(info.title),
            username=escape_html(info.display_name),
        ),
        reply_markup=back_keyboard(lang, CB.CHANNELS),
        parse_mode="HTML",
    )
    logger.info(
        "Channel %s connected by %s",
        channel.display_name,
        message.from_user.id if message.from_user else "?",
    )


@router.callback_query(F.data.startswith(f"{CB.CHANNEL_REMOVE}:"))
async def remove_channel(
    callback: CallbackQuery, session: AsyncSession, owner_id: int, lang: str = "uz"
) -> None:
    """Stop posting to a channel.

    Deactivates rather than deletes, so the delivery history and statistics that
    reference it stay intact.
    """
    raw_id = (callback.data or "").rsplit(":", 1)[-1]
    if not raw_id.isdigit():
        await answer_callback(callback, t("common.error", lang), alert=True)
        return

    channels = ChannelRepository(session, owner_id)
    channel = await channels.get(int(raw_id))
    if channel is None:
        await answer_callback(callback, t("common.error", lang), alert=True)
        return

    name = channel.display_name
    await channels.deactivate(channel)
    await EventRepository(session).record(
        EventType.CHANNEL_REMOVED,
        f"Channel removed: {name}",
        payload={"channel": name},
    )

    await answer_callback(callback, t("channel.removed", lang, name=name))
    await _render_channel_list(callback, session, owner_id, lang)
    logger.info("Channel %s deactivated", name)


async def _render_channel_list(
    callback: CallbackQuery, session: AsyncSession, owner_id: int, language: str
) -> None:
    """Draw the channel list with its keyboard."""
    channels = await ChannelRepository(session, owner_id).list_active()

    if channels:
        lines = [t("channel.current", language)]
        lines.extend(
            t(
                "channel.item",
                language,
                name=escape_html(channel.display_name),
                status=t("channel.status_active", language),
            )
            for channel in channels
        )
        body = "\n".join(lines)
    else:
        body = t("channel.none", language)

    await safe_edit(
        callback,
        f"{t('channel.title', language)}\n\n{body}",
        reply_markup=channels_keyboard(
            language, [(channel.id, channel.display_name) for channel in channels]
        ),
    )
