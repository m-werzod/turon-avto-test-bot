"""Statistics panel and the manual "Send now" action."""

from __future__ import annotations

from datetime import datetime

from aiogram import F, Router
from aiogram.types import CallbackQuery
from sqlalchemy.ext.asyncio import AsyncSession

from bot.database.models.quiz_post import PostTrigger
from bot.database.repositories import CycleExhaustedError, SettingsRepository
from bot.handlers.helpers import answer_callback, safe_edit
from bot.keyboards import CB, back_keyboard, confirm_keyboard, stats_keyboard
from bot.locales.i18n import t
from bot.services.quiz_service import NoChannelsError, QuizService
from bot.services.stats_service import StatsService
from bot.utils.logging import get_logger
from bot.utils.text import escape_html

logger = get_logger(__name__)

router = Router(name="admin-stats")


@router.callback_query(F.data.in_({CB.STATS, CB.STATS_REFRESH}))
async def show_stats(
    callback: CallbackQuery,
    session: AsyncSession,
    stats_service: StatsService,
    lang: str = "uz",
) -> None:
    """Render the statistics snapshot."""
    await answer_callback(callback)
    stats = await stats_service.collect(session)

    channels = (
        "\n".join(f"• {escape_html(name)}" for name in stats.channels)
        if stats.channels
        else t("stats.no_channels", lang)
    )

    if stats.last_sent_question_id is not None:
        last_sent = t(
            "stats.last_sent_item",
            lang,
            question_id=stats.last_sent_question_id,
            when=stats_service.format_local(stats.last_sent_at),
            preview=escape_html(stats.last_sent_preview or ""),
        )
    else:
        last_sent = t("stats.no_last_sent", lang)

    body = t(
        "stats.body",
        lang,
        total=stats.total_questions,
        with_images=stats.questions_with_images,
        cycle=stats.cycle_number,
        cycle_sent=stats.cycle_sent,
        cycle_remaining=stats.cycle_remaining,
        cycle_percent=stats.cycle_percent,
        sent_total=stats.sent_total,
        sent_today=stats.sent_today,
        failed_total=stats.failed_total,
        channels=channels,
        scheduler_status=t(
            "scheduler.paused" if stats.scheduler_paused else "scheduler.running", lang
        ),
        times=", ".join(stats.schedule_times) or t("common.none", lang),
        next_run=(
            t("scheduler.next_run_unknown", lang)
            if stats.scheduler_paused or not stats.schedule_times
            else ", ".join(stats.schedule_times)
        ),
        last_sent=last_sent,
    )

    # A refresh whose numbers are identical would be a no-op edit; the timestamp
    # guarantees the text differs so the admin sees the button did something.
    footer = t(
        "stats.refreshed", lang, time=datetime.now(stats_service.timezone).strftime("%H:%M:%S")
    )

    await safe_edit(
        callback,
        f"{t('stats.title', lang)}\n\n{body}\n\n<i>{footer}</i>",
        reply_markup=stats_keyboard(lang),
    )


@router.callback_query(F.data == CB.SEND_NOW)
async def confirm_send_now(callback: CallbackQuery, lang: str = "uz") -> None:
    """Ask before publishing off-schedule.

    A confirmation step because this posts to a live audience channel, and the
    button sits directly beside Statistics in the menu.
    """
    await answer_callback(callback)
    await safe_edit(
        callback,
        t("send_now.confirm", lang),
        reply_markup=confirm_keyboard(lang, CB.SEND_NOW_CONFIRM),
    )


@router.callback_query(F.data == CB.SEND_NOW_CONFIRM)
async def send_now(
    callback: CallbackQuery,
    session: AsyncSession,
    quiz_service: QuizService,
    lang: str = "uz",
) -> None:
    """Publish the next batch immediately, ignoring the schedule."""
    await answer_callback(callback)

    batch_size = await SettingsRepository(session).questions_per_send()
    await safe_edit(callback, t("send_now.sending", lang))

    try:
        reports = await quiz_service.send_batch(
            session, batch_size, trigger=PostTrigger.MANUAL, admin_language=lang
        )
    except NoChannelsError:
        await safe_edit(callback, t("send_now.no_channels", lang), reply_markup=back_keyboard(lang))
        return
    except CycleExhaustedError:
        await safe_edit(
            callback,
            t("send_now.no_questions", lang, update=t("menu.update_tests", lang)),
            reply_markup=back_keyboard(lang),
        )
        return
    except Exception as exc:
        logger.exception("Manual send failed")
        await safe_edit(
            callback,
            t("send_now.failed", lang, reason=escape_html(str(exc)[:300])),
            reply_markup=back_keyboard(lang),
        )
        return

    if not reports:
        # send_batch swallows an exhausted bank so earlier posts in the batch
        # survive; with nothing sent at all the cause is an empty bank.
        await safe_edit(
            callback,
            t("send_now.no_questions", lang, update=t("menu.update_tests", lang)),
            reply_markup=back_keyboard(lang),
        )
        return

    failed_reports = [report for report in reports if report.fully_failed]
    last = reports[-1]

    if len(failed_reports) == len(reports):
        text = t("send_now.failed", lang, reason=escape_html(last.error_summary()))
    elif failed_reports or any(report.failed for report in reports):
        text = t(
            "send_now.partial",
            lang,
            ok=sum(report.succeeded for report in reports),
            failed=sum(report.failed for report in reports),
            errors=escape_html(last.error_summary()),
        )
    elif len(reports) == 1:
        text = t(
            "send_now.success",
            lang,
            question_id=last.question_id,
            channels=last.succeeded,
            cycle=last.cycle_number,
        )
    else:
        text = t(
            "send_now.success_batch",
            lang,
            count=len(reports),
            channels=last.succeeded,
            cycle=last.cycle_number,
        )

    await safe_edit(callback, text, reply_markup=back_keyboard(lang))
