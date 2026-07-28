"""Shared repository behaviour."""

from __future__ import annotations

from typing import Any, cast

from sqlalchemy import CursorResult, delete, func, select
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
        result = cast("CursorResult[Any]", await self.session.execute(stmt))
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


class OwnedRepository[ModelT: Base](BaseRepository[ModelT]):
    """A repository over rows belonging to one Telegram user.

    The owner is supplied once, to the constructor, and every query scopes itself
    to it. The alternative — an ``owner_id`` argument on each of the three dozen
    methods — makes correctness a matter of remembering, and a single forgotten
    filter shows one person's channels, progress or settings to another. Here the
    unsafe version is the one that does not compile: there is no way to build the
    repository without saying whose data it is.

    ``model`` must carry :class:`~bot.database.base.OwnerMixin`.
    """

    def __init__(self, session: AsyncSession, owner_id: int) -> None:
        """Bind the repository to one owner.

        Args:
            session: Open session.
            owner_id: Telegram id whose rows this instance may see.

        Raises:
            ValueError: ``owner_id`` is falsy. A zero or ``None`` owner would
                quietly match nothing (or, worse, be treated as a real owner
                shared by every caller that forgot to pass one).
        """
        if not owner_id:
            raise ValueError(f"{type(self).__name__} requires a real owner_id, got {owner_id!r}")
        super().__init__(session)
        self.owner_id = owner_id

    def owned(self, stmt: Any) -> Any:
        """Add the owner filter to a statement.

        Every query in a subclass goes through this rather than writing the
        comparison by hand, so the filter is impossible to misspell and trivial
        to grep for when auditing.
        """
        return stmt.where(self.model.owner_id == self.owner_id)  # type: ignore[attr-defined]

    async def get(self, pk: int) -> ModelT | None:
        """Fetch one of *this owner's* rows by primary key.

        Overridden because ``Session.get`` bypasses the where clause entirely; a
        raw primary key from callback data would otherwise reach across owners.
        """
        stmt = self.owned(select(self.model).where(self.model.id == pk))  # type: ignore[attr-defined]
        return await self.session.scalar(stmt)

    async def list_all(self, *, limit: int | None = None, offset: int = 0) -> list[ModelT]:
        """Fetch this owner's rows in primary-key order."""
        stmt = self.owned(select(self.model)).order_by(self.model.id).offset(offset)  # type: ignore[attr-defined]
        if limit is not None:
            stmt = stmt.limit(limit)
        result = await self.session.scalars(stmt)
        return list(result)

    async def count(self) -> int:
        """Number of rows this owner has."""
        stmt = self.owned(select(func.count()).select_from(self.model))
        return int(await self.session.scalar(stmt) or 0)

    async def add(self, instance: ModelT) -> ModelT:
        """Stamp the owner onto a new row, then insert it.

        Done here rather than at each construction site for the same reason the
        filter is: a row created without an owner either violates NOT NULL — the
        lucky case — or, if the column ever became nullable, becomes invisible to
        every query and silently lost.
        """
        if getattr(instance, "owner_id", None) is None:
            instance.owner_id = self.owner_id  # type: ignore[attr-defined]
        return await super().add(instance)
