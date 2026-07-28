"""Shared repository behaviour."""

from __future__ import annotations

from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.database.base import Base


class BaseRepository[ModelT: Base]:
    """CRUD primitives shared by every concrete repository.

    Repositories accept a session rather than creating one, so a handler and the
    services it calls all take part in the same transaction and either commit
    together or roll back together.
    """

    model: type[ModelT]

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get(self, pk: int) -> ModelT | None:
        """Fetch one row by primary key, or ``None``."""
        return await self.session.get(self.model, pk)

    async def list_all(self, *, limit: int | None = None, offset: int = 0) -> list[ModelT]:
        """Fetch rows in primary-key order."""
        stmt = select(self.model).order_by(self.model.id).offset(offset)  # type: ignore[attr-defined]
        if limit is not None:
            stmt = stmt.limit(limit)
        result = await self.session.scalars(stmt)
        return list(result)

    async def count(self) -> int:
        """Total number of rows."""
        stmt = select(func.count()).select_from(self.model)
        return int(await self.session.scalar(stmt) or 0)

    async def add(self, instance: ModelT) -> ModelT:
        """Stage a new row and flush so its primary key is populated."""
        self.session.add(instance)
        await self.session.flush()
        return instance

    async def delete(self, instance: ModelT) -> None:
        """Remove a row."""
        await self.session.delete(instance)
        await self.session.flush()

    async def delete_by_id(self, pk: int) -> int:
        """Delete by primary key without loading the row.

        Returns:
            Number of rows removed: 1 if it existed, 0 otherwise.
        """
        stmt = delete(self.model).where(self.model.id == pk)  # type: ignore[attr-defined]
        result = await self.session.execute(stmt)
        await self.session.flush()
        return int(result.rowcount or 0)

    @staticmethod
    def apply_updates(instance: ModelT, **fields: Any) -> bool:
        """Assign ``fields`` onto ``instance``, skipping unchanged values.

        Returns:
            True when at least one attribute actually changed. Callers use this
            to avoid bumping ``updated_at`` on a no-op write.
        """
        changed = False
        for name, value in fields.items():
            if getattr(instance, name) != value:
                setattr(instance, name, value)
                changed = True
        return changed
