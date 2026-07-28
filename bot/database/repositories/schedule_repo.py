"""Schedule slot storage."""

from __future__ import annotations

from datetime import time

from sqlalchemy import delete, select

from bot.database.models.schedule import ScheduleSlot
from bot.database.repositories.base import BaseRepository
from bot.utils.logging import get_logger

logger = get_logger(__name__)


class ScheduleRepository(BaseRepository[ScheduleSlot]):
    """Daily posting times."""

    model = ScheduleSlot

    async def list_enabled(self) -> list[ScheduleSlot]:
        """Enabled slots in chronological order."""
        stmt = (
            select(ScheduleSlot)
            .where(ScheduleSlot.is_enabled.is_(True))
            .order_by(ScheduleSlot.run_at)
        )
        result = await self.session.scalars(stmt)
        return list(result)

    async def list_all_ordered(self) -> list[ScheduleSlot]:
        """Every slot, enabled or not, in chronological order."""
        stmt = select(ScheduleSlot).order_by(ScheduleSlot.run_at)
        result = await self.session.scalars(stmt)
        return list(result)

    async def get_by_time(self, run_at: time) -> ScheduleSlot | None:
        """Find a slot by its exact time."""
        return await self.session.scalar(
            select(ScheduleSlot).where(ScheduleSlot.run_at == run_at)
        )

    async def add_slot(self, run_at: time) -> tuple[ScheduleSlot, bool]:
        """Add a posting time, or re-enable it if it already exists.

        Returns:
            The slot and whether it was newly created.
        """
        existing = await self.get_by_time(run_at)
        if existing is not None:
            if not existing.is_enabled:
                existing.is_enabled = True
                await self.session.flush()
            return existing, False

        slot = ScheduleSlot(run_at=run_at, is_enabled=True)
        await self.add(slot)
        return slot, True

    async def replace_all(self, times: list[time]) -> list[ScheduleSlot]:
        """Make the stored schedule exactly ``times``.

        Used by the "posts per day" flow, where the admin re-enters the full set
        of times. Replacing wholesale avoids leaving an orphaned slot behind when
        the count shrinks from three to one.

        Args:
            times: Desired posting times. Duplicates are ignored.

        Returns:
            The resulting slots, in chronological order.
        """
        await self.session.execute(delete(ScheduleSlot))
        await self.session.flush()

        slots = [ScheduleSlot(run_at=value, is_enabled=True) for value in sorted(set(times))]
        self.session.add_all(slots)
        await self.session.flush()
        logger.info("Schedule replaced with %s", [slot.label for slot in slots])
        return slots

    async def remove_slot(self, run_at: time) -> bool:
        """Delete one posting time.

        Returns:
            Whether a slot was actually removed.
        """
        result = await self.session.execute(
            delete(ScheduleSlot).where(ScheduleSlot.run_at == run_at)
        )
        await self.session.flush()
        return bool(result.rowcount)
