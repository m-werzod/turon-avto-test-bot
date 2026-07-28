"""Async engine and session lifecycle."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from bot.utils.logging import get_logger

logger = get_logger(__name__)


class Database:
    """Owns the engine and hands out sessions.

    One instance lives for the process lifetime and is injected into handlers and
    jobs, which keeps a single connection pool shared across the whole app
    instead of each caller building its own.
    """

    def __init__(self, url: str, *, echo: bool = False, pool_size: int = 10) -> None:
        """Create the engine.

        Args:
            url: Async SQLAlchemy DSN.
            echo: Log every emitted statement. Debugging only — very noisy.
            pool_size: Persistent connections to keep open. Ignored by SQLite,
                which uses a non-queue pool.
        """
        self._url = url
        is_sqlite = url.startswith("sqlite")

        engine_kwargs: dict[str, object] = {
            "echo": echo,
            # Recycle before typical managed-Postgres idle timeouts, and verify a
            # connection before use so a link dropped overnight surfaces as a
            # transparent reconnect rather than a failed 08:00 post.
            "pool_pre_ping": True,
        }
        if not is_sqlite:
            engine_kwargs |= {
                "pool_size": pool_size,
                "max_overflow": pool_size,
                "pool_recycle": 1800,
            }

        self._engine: AsyncEngine = create_async_engine(url, **engine_kwargs)
        self._session_factory: async_sessionmaker[AsyncSession] = async_sessionmaker(
            bind=self._engine,
            class_=AsyncSession,
            expire_on_commit=False,  # objects stay usable after commit
            autoflush=False,
        )

    @property
    def engine(self) -> AsyncEngine:
        """The underlying engine, for migrations and health checks."""
        return self._engine

    @property
    def session_factory(self) -> async_sessionmaker[AsyncSession]:
        """Factory used by the middleware to open a per-update session."""
        return self._session_factory

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        """Yield a session inside a transaction.

        Commits on clean exit, rolls back on any exception, and always closes.
        Use this for background work (scheduler jobs, imports); request handlers
        receive their session from the middleware instead.
        """
        async with self._session_factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    async def healthcheck(self) -> bool:
        """Return whether the database answers a trivial query."""
        from sqlalchemy import text

        try:
            async with self._engine.connect() as connection:
                await connection.execute(text("SELECT 1"))
        except Exception as exc:  # noqa: BLE001 - health checks must not raise
            logger.error("Database healthcheck failed: %s", exc)
            return False
        return True

    async def dispose(self) -> None:
        """Close every pooled connection. Called during shutdown."""
        await self._engine.dispose()
        logger.info("Database connection pool disposed")
