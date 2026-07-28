"""APScheduler wiring, rebuilt from the database on every start.

Schedule state lives in the ``schedule_slots`` table, not in a persisted
APScheduler job store. On boot the scheduler reads those rows and registers cron
triggers for them, so a restart, a container rebuild or a database restore all
reproduce the same schedule — and the schedule remains plain SQL an operator can
read, rather than pickled job objects.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from apscheduler.executors.asyncio import AsyncIOExecutor
from apscheduler.jobstores.memory import MemoryJobStore
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from bot.database.models.event_log import EventType
from bot.database.models.quiz_post import PostTrigger
from bot.database.repositories import (
    DeliveryRepository,
    EventRepository,
    ScheduleRepository,
    SettingsRepository,
)
from bot.scheduler.jobs import JobContext, run_maintenance, run_scheduled_post
from bot.utils.logging import get_logger

logger = get_logger(__name__)

#: Prefix for per-slot job ids, so a reload can find and replace them.
_SLOT_JOB_PREFIX = "quiz_slot_"
_MAINTENANCE_JOB_ID = "nightly_maintenance"


class QuizScheduler:
    """Owns the APScheduler instance and keeps it in sync with the database."""

    def __init__(
        self,
        context: JobContext,
        *,
        timezone: ZoneInfo,
        misfire_grace: int = 3600,
    ) -> None:
        """Configure the scheduler.

        Args:
            context: Dependencies handed to every job.
            timezone: Zone all cron triggers are evaluated in. Explicitly not the
                server's local time — a VPS in UTC must still post at 08:00
                Tashkent.
            misfire_grace: How late a run may fire after its scheduled moment
                before being abandoned.
        """
        self.context = context
        self.timezone = timezone
        self.misfire_grace = misfire_grace

        self._scheduler = AsyncIOScheduler(
            jobstores={"default": MemoryJobStore()},
            executors={"default": AsyncIOExecutor()},
            job_defaults={
                # Collapse several missed runs of the same job into one: after a
                # long outage the channel should get the next quiz, not six at
                # once.
                "coalesce": True,
                "max_instances": 1,
                "misfire_grace_time": misfire_grace,
            },
            timezone=timezone,
        )

    @property
    def running(self) -> bool:
        """Whether the underlying scheduler is started."""
        return self._scheduler.running

    async def start(self) -> None:
        """Start the scheduler and load the stored schedule."""
        if not self._scheduler.running:
            self._scheduler.start()
            logger.info("APScheduler started (timezone=%s)", self.timezone)

        await self.reload()
        self._register_maintenance()

        async with self.context.db.session() as session:
            slots = await ScheduleRepository(session).list_enabled()
            paused = await SettingsRepository(session).is_scheduler_paused()
            await EventRepository(session).record(
                EventType.SCHEDULER_STARTED,
                f"Scheduler started with {len(slots)} slot(s): "
                f"{', '.join(slot.label for slot in slots) or 'none'}"
                + (" (paused)" if paused else ""),
                payload={
                    "slots": [slot.label for slot in slots],
                    "paused": paused,
                    "timezone": str(self.timezone),
                },
            )

        await self.catch_up_missed()

    async def reload(self) -> None:
        """Rebuild every quiz job from the database.

        Called at start-up and whenever an admin edits the schedule. Existing
        slot jobs are removed first so a deleted time cannot linger in memory.
        """
        for job in self._scheduler.get_jobs():
            if job.id.startswith(_SLOT_JOB_PREFIX):
                job.remove()

        async with self.context.db.session() as session:
            slots = await ScheduleRepository(session).list_enabled()

        for slot in slots:
            self._scheduler.add_job(
                run_scheduled_post,
                trigger=CronTrigger(
                    hour=slot.run_at.hour,
                    minute=slot.run_at.minute,
                    second=0,
                    timezone=self.timezone,
                ),
                id=f"{_SLOT_JOB_PREFIX}{slot.run_at.strftime('%H%M')}",
                name=f"Quiz post at {slot.label}",
                kwargs={"context": self.context, "slot_label": slot.label},
                replace_existing=True,
            )

        logger.info(
            "Scheduler reloaded with %d slot(s): %s",
            len(slots),
            ", ".join(slot.label for slot in slots) or "none",
        )

    def _register_maintenance(self) -> None:
        """Register the nightly housekeeping job."""
        self._scheduler.add_job(
            run_maintenance,
            trigger=CronTrigger(hour=3, minute=30, timezone=self.timezone),
            id=_MAINTENANCE_JOB_ID,
            name="Nightly maintenance",
            kwargs={"context": self.context},
            replace_existing=True,
        )

    async def catch_up_missed(self) -> None:
        """Publish a slot the bot slept through, when it is still recent.

        APScheduler computes the next fire time from *now* for a freshly
        registered job, so a run missed while the process was down would simply
        vanish. This restores the spec's "resume pending jobs" behaviour: if the
        most recent slot occurrence is inside the misfire grace window and
        nothing was delivered since, post once now.

        Deliberately conservative — it fires at most one catch-up, so a server
        that was off for a week does not dump a backlog into the channel.
        """
        now = datetime.now(self.timezone)

        async with self.context.db.session() as session:
            if await SettingsRepository(session).is_scheduler_paused():
                return

            slots = await ScheduleRepository(session).list_enabled()
            if not slots:
                return

            # Most recent occurrence of any slot, today or yesterday.
            occurrences: list[datetime] = []
            for slot in slots:
                today = now.replace(
                    hour=slot.run_at.hour, minute=slot.run_at.minute, second=0, microsecond=0
                )
                occurrences.append(today if today <= now else today - timedelta(days=1))

            latest = max(occurrences)
            age = (now - latest).total_seconds()
            if age > self.misfire_grace:
                logger.debug(
                    "No catch-up: last slot was %.0fs ago, beyond the %ds grace window",
                    age,
                    self.misfire_grace,
                )
                return

            last_delivery = await DeliveryRepository(session).last_sent()
            if last_delivery is not None and last_delivery.sent_at is not None:
                sent_at = last_delivery.sent_at
                if sent_at.tzinfo is None:
                    sent_at = sent_at.replace(tzinfo=UTC)
                if sent_at >= latest.astimezone(UTC):
                    logger.debug("No catch-up needed: a quiz was already sent for this slot")
                    return

        logger.info(
            "Catching up the %s slot missed while the bot was down",
            latest.strftime("%H:%M"),
        )
        await run_scheduled_post(
            self.context,
            slot_label=f"{latest.strftime('%H:%M')} (catch-up)",
            trigger=PostTrigger.CATCHUP,
        )

    def next_run_time(self) -> datetime | None:
        """When the next quiz job is due, or ``None`` if nothing is scheduled."""
        times = [
            job.next_run_time
            for job in self._scheduler.get_jobs()
            if job.id.startswith(_SLOT_JOB_PREFIX) and job.next_run_time is not None
        ]
        return min(times) if times else None

    def format_next_run(self) -> str | None:
        """Next run rendered as ``dd.mm.YYYY HH:MM`` in the configured zone."""
        moment = self.next_run_time()
        if moment is None:
            return None
        return moment.astimezone(self.timezone).strftime("%d.%m.%Y %H:%M")

    async def pause(self) -> None:
        """Suspend automatic posting and persist the decision.

        The flag is stored in the database rather than only in APScheduler, so a
        paused bot stays paused across a restart instead of quietly resuming.
        """
        async with self.context.db.session() as session:
            await SettingsRepository(session).set_scheduler_paused(True)
            await EventRepository(session).record(
                EventType.SCHEDULER_PAUSED, "Scheduler paused by admin"
            )
        logger.info("Scheduler paused")

    async def resume(self) -> None:
        """Resume automatic posting and persist the decision."""
        async with self.context.db.session() as session:
            await SettingsRepository(session).set_scheduler_paused(False)
            await EventRepository(session).record(
                EventType.SCHEDULER_RESUMED, "Scheduler resumed by admin"
            )
        logger.info("Scheduler resumed")

    async def is_paused(self) -> bool:
        """Whether automatic posting is currently suspended."""
        async with self.context.db.session() as session:
            return await SettingsRepository(session).is_scheduler_paused()

    async def shutdown(self) -> None:
        """Stop the scheduler, letting running jobs finish."""
        if self._scheduler.running:
            self._scheduler.shutdown(wait=True)
            logger.info("APScheduler stopped")
