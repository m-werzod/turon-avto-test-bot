"""The work the scheduler actually performs.

Jobs are plain coroutines taking an explicit context rather than reaching for
globals, which is what lets the test suite and the "Send now" button drive the
identical code path the 08:00 trigger does.

Every job swallows its own exceptions. An escaping error would let APScheduler
mark the job broken and stop future runs, which is precisely the "bot stops
posting and nobody notices" failure the spec rules out.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from bot.database.models.event_log import EventType
from bot.database.models.quiz_post import PostTrigger
from bot.database.repositories import (
    CycleExhaustedError,
    EventRepository,
    SettingsRepository,
)
from bot.database.session import Database
from bot.services.notify_service import NotifyService
from bot.services.quiz_service import NoChannelsError, QuizService, SendReport
from bot.utils.logging import get_logger

logger = get_logger(__name__)

#: Days of event-log history retained by the maintenance job.
EVENT_LOG_RETENTION_DAYS = 90

#: Backup archives kept on disk.
BACKUP_RETENTION_COUNT = 10


@dataclass(slots=True)
class JobContext:
    """Everything a job needs, passed in explicitly."""

    db: Database
    quiz: QuizService
    notify: NotifyService
    timezone: ZoneInfo
    admin_language: str = "uz"
    backup_service: object | None = None


async def run_scheduled_post(
    context: JobContext,
    *,
    slot_label: str = "",
    trigger: PostTrigger = PostTrigger.SCHEDULED,
) -> list[SendReport]:
    """Publish this slot's quizzes, honouring the pause and weekend settings.

    Args:
        context: Injected dependencies.
        slot_label: The ``HH:MM`` slot this run belongs to, for logging.
        trigger: Recorded on the resulting posts.

    Returns:
        One report per question sent — empty when the run was skipped or failed.
    """
    where = f"scheduler:{slot_label or trigger.value}"

    try:
        async with context.db.session() as session:
            settings_repo = SettingsRepository(session)

            if await settings_repo.is_scheduler_paused():
                logger.info("Slot %s skipped: scheduler is paused", slot_label)
                return []

            if await settings_repo.skip_weekends():
                weekday = datetime.now(context.timezone).weekday()
                if weekday >= 5:  # Saturday=5, Sunday=6
                    logger.info("Slot %s skipped: weekend posting is disabled", slot_label)
                    return []

            batch_size = await settings_repo.questions_per_send()
            reports = await context.quiz.send_batch(
                session,
                batch_size,
                trigger=trigger,
                admin_language=context.admin_language,
            )

    except NoChannelsError:
        logger.warning("Slot %s skipped: no active channels are connected", slot_label)
        async with context.db.session() as session:
            await EventRepository(session).record(
                EventType.QUIZ_FAILED,
                "Scheduled post skipped — no active channels are connected",
                level="WARNING",
                payload={"slot": slot_label},
            )
        await context.notify.broadcast(
            "⚠️ Rejadagi test yuborilmadi: birorta kanal ulanmagan.",
            fingerprint="no_channels",
        )
        return []

    except CycleExhaustedError as exc:
        logger.error("Slot %s failed: %s", slot_label, exc)
        async with context.db.session() as session:
            await EventRepository(session).record(
                EventType.QUIZ_FAILED,
                f"Scheduled post failed — {exc}",
                level="ERROR",
                payload={"slot": slot_label},
            )
        await context.notify.notify_error(where, exc, language=context.admin_language)
        return []

    except Exception as exc:
        logger.exception("Unhandled error in scheduled post (slot %s)", slot_label)
        await context.notify.notify_error(where, exc, language=context.admin_language)
        return []

    for report in reports:
        if report.fully_failed:
            await context.notify.broadcast(
                f"⚠️ Test hech bir kanalga yuborilmadi.\n\n{report.error_summary()}",
                fingerprint="all_channels_failed",
            )
        elif report.failed:
            for outcome in report.outcomes:
                if outcome.access_lost:
                    await context.notify.notify_channel_lost(
                        outcome.channel, outcome.error or "", language=context.admin_language
                    )

    return reports


async def run_maintenance(context: JobContext) -> None:
    """Nightly housekeeping: trim the event log and old backups.

    Without this the audit table grows without bound on a long-running
    deployment, and backups fill the disk.
    """
    try:
        async with context.db.session() as session:
            removed = await EventRepository(session).purge_older_than(EVENT_LOG_RETENTION_DAYS)
        if removed:
            logger.info("Maintenance: purged %d old event log row(s)", removed)

        backup_service = context.backup_service
        if backup_service is not None and hasattr(backup_service, "prune"):
            pruned = backup_service.prune(BACKUP_RETENTION_COUNT)
            if pruned:
                logger.info("Maintenance: pruned %d old backup archive(s)", pruned)

    except Exception as exc:
        logger.exception("Maintenance job failed")
        await context.notify.notify_error(
            "scheduler:maintenance", exc, language=context.admin_language
        )
