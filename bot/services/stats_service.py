"""Statistics shown in the admin panel."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, tzinfo
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncSession

from bot.database.repositories import (
    ChannelRepository,
    CycleRepository,
    DeliveryRepository,
    QuestionRepository,
    ScheduleRepository,
    SettingsRepository,
)
from bot.utils.logging import get_logger

logger = get_logger(__name__)


@dataclass(slots=True)
class Statistics:
    """A snapshot of the bot's state."""

    total_questions: int = 0
    questions_with_images: int = 0
    cycle_number: int = 0
    cycle_sent: int = 0
    cycle_remaining: int = 0
    sent_total: int = 0
    sent_today: int = 0
    failed_total: int = 0
    channels: list[str] = field(default_factory=list)
    scheduler_paused: bool = False
    schedule_times: list[str] = field(default_factory=list)
    last_sent_question_id: int | None = None
    last_sent_at: datetime | None = None
    last_sent_preview: str | None = None

    @property
    def cycle_percent(self) -> int:
        """Completion of the current cycle, 0-100."""
        total = self.cycle_sent + self.cycle_remaining
        if total <= 0:
            return 0
        return round(self.cycle_sent / total * 100)


class StatsService:
    """Builds the statistics snapshot."""

    def __init__(self, timezone: tzinfo | None = None) -> None:
        """Args:
        timezone: Zone that defines "today". Defaults to Asia/Tashkent.
        """
        self.timezone = timezone or ZoneInfo("Asia/Tashkent")

    async def collect(self, session: AsyncSession) -> Statistics:
        """Gather every figure the statistics panel displays.

        Args:
            session: Open session.

        Returns:
            The snapshot.
        """
        questions = QuestionRepository(session)
        cycles = CycleRepository(session)
        deliveries = DeliveryRepository(session)
        channels = ChannelRepository(session)
        schedule = ScheduleRepository(session)
        settings_repo = SettingsRepository(session)

        stats = Statistics()

        stats.total_questions = await questions.count_active()
        stats.questions_with_images = await questions.count_with_images()

        language = await settings_repo.content_language()
        cycle = await cycles.get_open_cycle()
        if cycle is not None:
            stats.cycle_number = cycle.number
            stats.cycle_sent = await cycles.count_posts_in_cycle(cycle.id)
            remaining = await cycles.count_remaining(cycle.id, language)
            if remaining == 0 and stats.total_questions:
                # No questions in the configured language; report against the
                # whole bank so the panel is not misleadingly empty.
                remaining = await cycles.count_remaining(cycle.id)
            stats.cycle_remaining = remaining

        stats.sent_total = await deliveries.count_sent()
        stats.failed_total = await deliveries.count_failed()
        today = datetime.now(self.timezone).date()
        stats.sent_today = await deliveries.count_sent_on(today, self.timezone)

        stats.channels = [channel.display_name for channel in await channels.list_active()]

        stats.scheduler_paused = await settings_repo.is_scheduler_paused()
        stats.schedule_times = [slot.label for slot in await schedule.list_enabled()]

        last = await deliveries.last_sent()
        if last is not None:
            stats.last_sent_at = last.sent_at
            question = last.post.question if last.post else None
            if question is not None:
                stats.last_sent_question_id = question.id
                stats.last_sent_preview = (
                    question.text[:80] + "…" if len(question.text) > 80 else question.text
                )

        return stats

    def format_local(self, moment: datetime | None) -> str:
        """Render a UTC timestamp in the configured timezone."""
        if moment is None:
            return "-"
        if moment.tzinfo is None:
            from datetime import UTC

            moment = moment.replace(tzinfo=UTC)
        return moment.astimezone(self.timezone).strftime("%d.%m.%Y %H:%M")
