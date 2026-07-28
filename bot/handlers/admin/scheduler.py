"""Schedule configuration, pause and resume."""

from __future__ import annotations

import re
from datetime import time

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot.database.models.event_log import EventType
from bot.database.models.setting import SettingKey
from bot.database.repositories import EventRepository, ScheduleRepository, SettingsRepository
from bot.handlers.helpers import answer_callback, safe_edit
from bot.keyboards import (
    CB,
    back_keyboard,
    batch_size_keyboard,
    posts_per_day_keyboard,
    scheduler_keyboard,
)
from bot.locales.i18n import t
from bot.scheduler.scheduler import QuizScheduler
from bot.states import ScheduleStates
from bot.utils.logging import get_logger

logger = get_logger(__name__)

router = Router(name="admin-scheduler")

#: Accepts 8:00, 08:00, 08.00 and 08 00 — an admin typing on a phone should not
#: have to guess which separator the bot wants.
_TIME_PATTERN = re.compile(r"^([01]?\d|2[0-3])[:.\s]([0-5]\d)$")


def _parse_times(raw: str) -> tuple[list[time], list[str]]:
    """Parse a free-text list of times.

    Args:
        raw: Times separated by newlines, commas or semicolons.

    Returns:
        The parsed times in order, and the tokens that could not be parsed.
    """
    tokens = [token.strip() for token in re.split(r"[\n,;]+", raw) if token.strip()]
    parsed: list[time] = []
    invalid: list[str] = []

    for token in tokens:
        match = _TIME_PATTERN.match(token)
        if match is None:
            invalid.append(token)
            continue
        parsed.append(time(hour=int(match.group(1)), minute=int(match.group(2))))

    return parsed, invalid


@router.callback_query(F.data == CB.SCHED)
async def show_scheduler(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    scheduler: QuizScheduler,
    lang: str = "uz",
) -> None:
    """Show the current schedule."""
    await state.clear()
    await answer_callback(callback)
    await _render_scheduler(callback, session, scheduler, lang)


@router.callback_query(F.data == CB.SCHED_EDIT)
async def choose_count(callback: CallbackQuery, lang: str = "uz") -> None:
    """Ask how many posts per day."""
    await answer_callback(callback)
    await safe_edit(
        callback, t("scheduler.choose_count", lang), reply_markup=posts_per_day_keyboard(lang)
    )


@router.callback_query(F.data.startswith(f"{CB.SCHED_COUNT}:"))
async def prompt_for_times(callback: CallbackQuery, state: FSMContext, lang: str = "uz") -> None:
    """Ask for the exact times."""
    raw_count = (callback.data or "").rsplit(":", 1)[-1]
    if not raw_count.isdigit() or not 1 <= int(raw_count) <= 3:
        await answer_callback(callback, t("common.error", lang), alert=True)
        return

    count = int(raw_count)
    await state.set_state(ScheduleStates.waiting_for_times)
    await state.update_data(expected_count=count)
    await answer_callback(callback)
    await safe_edit(
        callback,
        t("scheduler.enter_times", lang, count=count),
        reply_markup=back_keyboard(lang, CB.SCHED),
    )


@router.message(ScheduleStates.waiting_for_times, F.text)
async def receive_times(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    scheduler: QuizScheduler,
    lang: str = "uz",
) -> None:
    """Validate and store the posting times, then reload the scheduler."""
    data = await state.get_data()
    expected = int(data.get("expected_count", 0))

    parsed, invalid = _parse_times(message.text or "")

    if invalid:
        await message.answer(t("scheduler.invalid_time", lang, value=invalid[0]), parse_mode="HTML")
        return

    if expected and len(parsed) != expected:
        await message.answer(t("scheduler.wrong_count", lang, expected=expected, got=len(parsed)))
        return

    if len(set(parsed)) != len(parsed):
        await message.answer(t("scheduler.duplicate_times", lang))
        return

    schedule = ScheduleRepository(session)
    slots = await schedule.replace_all(parsed)
    await SettingsRepository(session).set_int(SettingKey.POSTS_PER_DAY, len(slots))

    labels = ", ".join(slot.label for slot in slots)
    await EventRepository(session).record(
        EventType.SCHEDULE_UPDATED,
        f"Schedule set to {labels}",
        payload={"times": [slot.label for slot in slots]},
    )

    # Commit before reloading: the scheduler opens its own session to read the
    # slots, and would otherwise see the pre-update schedule.
    await session.commit()
    await scheduler.reload()

    await state.clear()
    await message.answer(
        t("scheduler.saved", lang, times=labels, timezone=str(scheduler.timezone)),
        parse_mode="HTML",
        reply_markup=back_keyboard(lang, CB.SCHED),
    )
    logger.info("Schedule updated to %s", labels)


@router.callback_query(F.data == CB.SCHED_PAUSE)
async def pause_scheduler(
    callback: CallbackQuery, session: AsyncSession, scheduler: QuizScheduler, lang: str = "uz"
) -> None:
    """Suspend automatic posting."""
    settings_repo = SettingsRepository(session)
    if await settings_repo.is_scheduler_paused():
        await answer_callback(callback, t("scheduler.already_paused", lang), alert=True)
        return

    await session.commit()  # the scheduler writes through its own session
    await scheduler.pause()

    await answer_callback(callback)
    await safe_edit(
        callback,
        t("scheduler.paused_ok", lang, resume=t("menu.resume", lang)),
        reply_markup=back_keyboard(lang),
    )


@router.callback_query(F.data == CB.SCHED_RESUME)
async def resume_scheduler(
    callback: CallbackQuery, session: AsyncSession, scheduler: QuizScheduler, lang: str = "uz"
) -> None:
    """Resume automatic posting."""
    settings_repo = SettingsRepository(session)
    if not await settings_repo.is_scheduler_paused():
        await answer_callback(callback, t("scheduler.already_running", lang), alert=True)
        return

    await session.commit()
    await scheduler.resume()

    next_run = scheduler.format_next_run()
    next_line = (
        t("scheduler.next_run", lang, time=next_run)
        if next_run
        else t("scheduler.next_run_unknown", lang)
    )

    await answer_callback(callback)
    await safe_edit(
        callback,
        t("scheduler.resumed_ok", lang, next_run=next_line),
        reply_markup=back_keyboard(lang),
    )


@router.callback_query(F.data == CB.SCHED_BATCH)
async def choose_batch_size(
    callback: CallbackQuery, session: AsyncSession, lang: str = "uz"
) -> None:
    """Offer the batch sizes."""
    current = await SettingsRepository(session).questions_per_send()
    await safe_edit(
        callback,
        t("scheduler.batch_prompt", lang, current=current),
        reply_markup=batch_size_keyboard(lang, current),
    )


@router.callback_query(F.data.startswith(f"{CB.SCHED_BATCH_SET}:"))
async def set_batch_size(
    callback: CallbackQuery, session: AsyncSession, scheduler: QuizScheduler, lang: str = "uz"
) -> None:
    """Persist how many questions go out at each scheduled time."""
    raw = (callback.data or "").rsplit(":", 1)[-1]
    try:
        count = int(raw)
    except ValueError:
        await answer_callback(callback, t("errors.generic", lang))
        return

    await SettingsRepository(session).set_questions_per_send(count)
    await answer_callback(callback, t("scheduler.batch_saved", lang, count=count))
    await _render_scheduler(callback, session, scheduler, lang)


@router.callback_query(F.data == CB.SCHED_WEEKENDS)
async def toggle_weekends(
    callback: CallbackQuery, session: AsyncSession, scheduler: QuizScheduler, lang: str = "uz"
) -> None:
    """Turn weekend posting on or off."""
    settings_repo = SettingsRepository(session)
    skip_now = not await settings_repo.skip_weekends()
    await settings_repo.set_skip_weekends(skip_now)

    status = t("settings.weekends_skip" if skip_now else "settings.weekends_send", lang)
    await answer_callback(callback, t("settings.weekends_saved", lang, status=status))
    await _render_scheduler(callback, session, scheduler, lang)


async def _render_scheduler(
    callback: CallbackQuery, session: AsyncSession, scheduler: QuizScheduler, language: str
) -> None:
    """Draw the scheduler panel."""
    settings_repo = SettingsRepository(session)
    paused = await settings_repo.is_scheduler_paused()
    skip_weekends = await settings_repo.skip_weekends()
    per_send = await settings_repo.questions_per_send()
    slots = await ScheduleRepository(session).list_enabled()

    times = ", ".join(slot.label for slot in slots)
    next_run = scheduler.format_next_run()

    lines = [
        t("scheduler.title", language),
        "",
        t(
            "scheduler.status_line",
            language,
            status=t("scheduler.paused" if paused else "scheduler.running", language),
        ),
        (
            t("scheduler.current_times", language, times=times)
            if slots
            else t("scheduler.no_times", language)
        ),
        (
            t("scheduler.next_run", language, time=next_run)
            if next_run and not paused
            else t("scheduler.next_run_unknown", language)
        ),
        t("scheduler.batch_line", language, count=per_send, total=len(slots) * per_send),
        t(
            "scheduler.skip_weekends",
            language,
            status=t(
                "settings.weekends_skip" if skip_weekends else "settings.weekends_send",
                language,
            ),
        ),
        "",
        t("scheduler.timezone_note", language, timezone=str(scheduler.timezone)),
    ]

    await safe_edit(
        callback, "\n".join(lines), reply_markup=scheduler_keyboard(language, paused=paused)
    )
