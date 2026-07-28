"""Schedule slot storage."""

from __future__ import annotations

from datetime import time
from typing import Any, cast

from sqlalchemy import CursorResult, delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.database.models.schedule import ScheduleSlot
from bot.database.repositories.base import OwnedRepository
from bot.utils.logging import get_logger

logger = get_logger(__name__)


class ScheduleRepository(OwnedRepository[ScheduleSlot]):
    """Daily posting times."""

    model = ScheduleSlot

    async def list_enabled(self) -> list[ScheduleSlot]:
        """Enabled slots in chronological order."""
        stmt = (
            self.owned(select(ScheduleSlot))
            .where(ScheduleSlot.is_enabled.is_(True))
            .order_by(ScheduleSlot.run_at)
        )
        result = await self.session.scalars(stmt)
        return list(result)

    async def list_all_ordered(self) -> list[ScheduleSlot]:
        """Every slot, enabled or not, in chronological order."""
        stmt = self.owned(select(ScheduleSlot)).order_by(ScheduleSlot.run_at)
        result = await self.session.scalars(stmt)
        return list(result)

    async def get_by_time(self, run_at: time) -> ScheduleSlot | None:
        """Find a slot by its exact time."""
        return await self.session.scalar(
            self.owned(select(ScheduleSlot).where(ScheduleSlot.run_at == run_at))
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
        # Scoped, emphatically: an unqualified DELETE here would wipe every
        # other user's schedule the moment one person edited their own.
        await self.session.execute(self.owned(delete(ScheduleSlot)))
        await self.session.flush()

        slots = [
            ScheduleSlot(run_at=value, is_enabled=True, owner_id=self.owner_id)
            for value in sorted(set(times))
        ]
        self.session.add_all(slots)
        await self.session.flush()
        logger.info("Schedule replaced with %s", [slot.label for slot in slots])
        return slots

    async def remove_slot(self, run_at: time) -> bool:
        """Delete one posting time.

        Returns:
            Whether a slot was actually removed.
        """
        result = cast(
            "CursorResult[Any]",
            await self.session.execute(
                self.owned(delete(ScheduleSlot).where(ScheduleSlot.run_at == run_at))
            ),
        )
        await self.session.flush()
        return bool(result.rowcount)

    @staticmethod
    async def owners_due_at(session: AsyncSession, run_at: time) -> list[int]:
        """Every owner with an enabled slot at ``run_at``.

        Deliberately a static method reaching across owners: the scheduler runs
        one cron job per distinct time and needs to know who it fires for. Kept
        here, and named so its breadth is obvious, rather than letting the
        scheduler assemble a raw cross-owner query of its own.

        Args:
            session: Open session.
            run_at: Wall-clock time of the slot.

        Returns:
            Telegram ids, ascending.
        """
        stmt = (
            select(ScheduleSlot.owner_id)
            .where(ScheduleSlot.run_at == run_at, ScheduleSlot.is_enabled.is_(True))
            .distinct()
            .order_by(ScheduleSlot.owner_id)
        )
        result = await session.scalars(stmt)
        return list(result)

    @staticmethod
    async def distinct_enabled_times(session: AsyncSession) -> list[time]:
        """Every wall-clock time any owner posts at.

        One cron job is registered per entry, so this collapses a thousand users
        sharing 08:00 into a single trigger instead of a thousand.
        """
        stmt = (
            select(ScheduleSlot.run_at)
            .where(ScheduleSlot.is_enabled.is_(True))
            .distinct()
            .order_by(ScheduleSlot.run_at)
        )
        result = await session.scalars(stmt)
        return list(result)

    @staticmethod
    async def distinct_owners(session: AsyncSession) -> list[int]:
        """Every owner who has any enabled slot.

        Used at start-up to decide whose missed run to catch up.
        """
        stmt = (
            select(ScheduleSlot.owner_id)
            .where(ScheduleSlot.is_enabled.is_(True))
            .distinct()
            .order_by(ScheduleSlot.owner_id)
        )
        result = await session.scalars(stmt)
        return list(result)
