"""APScheduler wiring, rebuilt from the database on every start.

Schedule state lives in the ``schedule_slots`` table, not in a persisted
APScheduler job store. On boot the scheduler reads those rows and registers cron
triggers for them, so a restart, a container rebuild or a database restore all
reproduce the same schedule — and the schedule remains plain SQL an operator can
read, rather than pickled job objects.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from apscheduler.executors.asyncio import AsyncIOExecutor
from apscheduler.jobstores.memory import MemoryJobStore
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from bot.config.settings import PROJECT_ROOT
from bot.database.models.event_log import EventType
from bot.database.models.quiz_post import PostTrigger
from bot.database.repositories import (
    DeliveryRepository,
    EventRepository,
    ScheduleRepository,
    SettingsRepository,
)
from bot.scheduler.jobs import (
    JobContext,
    run_maintenance,
    run_scheduled_post,
    run_slot,
    write_heartbeat,
)
from bot.utils.logging import get_logger

logger = get_logger(__name__)

#: Prefix for per-slot job ids, so a reload can find and replace them.
_SLOT_JOB_PREFIX = "quiz_slot_"
_MAINTENANCE_JOB_ID = "nightly_maintenance"
_HEARTBEAT_JOB_ID = "heartbeat"


class QuizScheduler:
    """Owns the APScheduler instance and keeps it in sync with the database."""

    def __init__(
        self,
        context: JobContext,
        *,
        timezone: ZoneInfo,
        misfire_grace: int = 3600,
        heartbeat_path: Path | None = None,
    ) -> None:
        """Configure the scheduler.

        Args:
            context: Dependencies handed to every job.
            timezone: Zone all cron triggers are evaluated in. Explicitly not the
                server's local time — a VPS in UTC must still post at 08:00
                Tashkent.
            misfire_grace: How late a run may fire after its scheduled moment
                before being abandoned.
            heartbeat_path: File touched every minute to prove the event loop is
                alive. Defaults to ``logs/heartbeat`` beside the project.
        """
        self.context = context
        self.timezone = timezone
        self.misfire_grace = misfire_grace
        self.heartbeat_path = heartbeat_path or (PROJECT_ROOT / "logs" / "heartbeat")

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
        self._register_heartbeat()

        async with self.context.db.session() as session:
            times = await ScheduleRepository.distinct_enabled_times(session)
            labels = [run_at.strftime("%H:%M") for run_at in times]
            await EventRepository(session).record(
                EventType.SCHEDULER_STARTED,
                f"Scheduler started with {len(times)} distinct time(s): "
                f"{', '.join(labels) or 'none'}",
                payload={"times": labels, "timezone": str(self.timezone)},
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

        # One job per distinct wall-clock time, not per slot: every owner posting
        # at 08:00 shares a single trigger, which fans out to them when it fires.
        # A job each would put thousands of near-identical cron entries in the
        # scheduler as the bot gains users, all waking at the same instant.
        async with self.context.db.session() as session:
            times = await ScheduleRepository.distinct_enabled_times(session)

        for run_at in times:
            label = run_at.strftime("%H:%M")
            self._scheduler.add_job(
                run_slot,
                trigger=CronTrigger(
                    hour=run_at.hour,
                    minute=run_at.minute,
                    second=0,
                    timezone=self.timezone,
                ),
                id=f"{_SLOT_JOB_PREFIX}{run_at.strftime('%H%M')}",
                name=f"Quiz post at {label}",
                kwargs={"context": self.context, "run_at": run_at, "slot_label": label},
                replace_existing=True,
            )

        logger.info(
            "Scheduler reloaded with %d distinct time(s): %s",
            len(times),
            ", ".join(run_at.strftime("%H:%M") for run_at in times) or "none",
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

    def _register_heartbeat(self) -> None:
        """Register the liveness heartbeat.

        Every minute, so an external watchdog can call the bot stuck after a few
        missed beats without being trigger-happy about one slow tick.
        """
        self._scheduler.add_job(
            write_heartbeat,
            trigger=IntervalTrigger(minutes=1, timezone=self.timezone),
            id=_HEARTBEAT_JOB_ID,
            name="Liveness heartbeat",
            kwargs={"context": self.context, "path": self.heartbeat_path},
            replace_existing=True,
        )

    async def catch_up_missed(self) -> None:
        """Publish a slot the bot slept through, when it is still recent.

        APScheduler computes the next fire time from *now* for a freshly
        registered job, so a run missed while the process was down would simply
        vanish. This restores the spec's "resume pending jobs" behaviour: if the
        most recent slot occurrence is inside the misfire grace window and
        nothing was delivered since, post once now.

        Deliberately conservative — at most one catch-up per owner, so a server
        that was off for a week does not dump a backlog into anybody's channel.
        """
        async with self.context.db.session() as session:
            owners = await ScheduleRepository.distinct_owners(session)

        for owner_id in owners:
            try:
                await self._catch_up_owner(owner_id)
            except Exception:
                # One owner's broken catch-up must not abort start-up for the
                # rest, and start-up is exactly when nobody is watching.
                logger.exception("Catch-up failed for owner %d", owner_id)

    async def _catch_up_owner(self, owner_id: int) -> None:
        """Run the catch-up check for a single owner."""
        now = datetime.now(self.timezone)

        async with self.context.db.session() as session:
            if await SettingsRepository(session, owner_id).is_scheduler_paused():
                return

            slots = await ScheduleRepository(session, owner_id).list_enabled()
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

            last_delivery = await DeliveryRepository(session).last_sent(owner_id=owner_id)
            if last_delivery is not None and last_delivery.sent_at is not None:
                sent_at = last_delivery.sent_at
                if sent_at.tzinfo is None:
                    sent_at = sent_at.replace(tzinfo=UTC)
                if sent_at >= latest.astimezone(UTC):
                    logger.debug("No catch-up needed: a quiz was already sent for this slot")
                    return

        logger.info(
            "Catching up owner %d's %s slot missed while the bot was down",
            owner_id,
            latest.strftime("%H:%M"),
        )
        await run_scheduled_post(
            self.context,
            owner_id=owner_id,
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

    async def pause(self, owner_id: int) -> None:
        """Suspend automatic posting and persist the decision.

        The flag is stored in the database rather than only in APScheduler, so a
        paused bot stays paused across a restart instead of quietly resuming.
        """
        async with self.context.db.session() as session:
            await SettingsRepository(session, owner_id).set_scheduler_paused(True)
            await EventRepository(session).record(
                EventType.SCHEDULER_PAUSED, "Scheduler paused by admin"
            )
        logger.info("Scheduler paused")

    async def resume(self, owner_id: int) -> None:
        """Resume automatic posting and persist the decision."""
        async with self.context.db.session() as session:
            await SettingsRepository(session, owner_id).set_scheduler_paused(False)
            await EventRepository(session).record(
                EventType.SCHEDULER_RESUMED, "Scheduler resumed by admin"
            )
        logger.info("Scheduler resumed")

    async def is_paused(self, owner_id: int) -> bool:
        """Whether automatic posting is currently suspended."""
        async with self.context.db.session() as session:
            return await SettingsRepository(session, owner_id).is_scheduler_paused()

    async def shutdown(self) -> None:
        """Stop the scheduler, letting running jobs finish."""
        if self._scheduler.running:
            self._scheduler.shutdown(wait=True)
            logger.info("APScheduler stopped")
