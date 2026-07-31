"""Refusing to start when another copy is already running.

Telegram allows exactly one ``getUpdates`` poller per token. A second copy does
not fail loudly — both processes take turns being kicked off, the bot answers
roughly half the time, and the logs fill with ``TelegramConflictError`` while the
supervisor reports everything as healthy.

It is an easy state to reach: a supervisor restarting a process that has not
finished dying, a deploy that leaves the old service enabled, a developer running
``python -m bot`` next to a running unit. So the second copy is stopped here, at
start-up, with an explanation.

Two mechanisms, chosen by backend:

* **PostgreSQL** — a session-level advisory lock. Held on the connection, so it
  is released the moment the process dies, however it dies, and it works across
  machines pointing at one database.
* **SQLite** — an OS lock on a sidecar file. Also released automatically by the
  kernel on exit, which matters because the common case is a crashed process
  leaving a stale marker behind.

A PID file would have needed neither, and would have been wrong: PIDs are reused,
and a stale file means either refusing to start forever or ignoring the lock. The
lock file here holds no content at all — it exists only to be locked. To find the
process actually holding it, ask the supervisor: ``systemctl status turon-bot``
or ``supervisorctl status turon-bot``.
"""

from __future__ import annotations

import contextlib
import os
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from bot.utils.logging import get_logger

logger = get_logger(__name__)

#: Arbitrary but fixed key for the PostgreSQL advisory lock. Derived from the
#: project name so an unrelated application sharing the database cannot collide
#: with it by accident.
ADVISORY_LOCK_KEY = 0x7503_0A70  # "turon avto" as a hex-ish mnemonic


class AlreadyRunningError(RuntimeError):
    """Another instance holds the lock."""

    def __init__(self, detail: str) -> None:
        super().__init__(
            f"Another instance of the bot is already running ({detail}).\n"
            "Telegram permits one poller per token; a second copy makes both "
            "unreliable.\n"
            "Stop the other one first:\n"
            "  systemd:    sudo systemctl stop turon-bot\n"
            "  supervisor: sudo supervisorctl stop turon-bot\n"
            "  docker:     docker compose stop bot\n"
            "  manual:     pkill -f 'python -m bot'"
        )


class _PostgresLock:
    """Session-level advisory lock held on a dedicated connection."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine
        self._connection: Any = None

    async def acquire(self) -> None:
        """Take the lock, or raise if somebody else has it."""
        connection = await self._engine.connect()
        try:
            acquired = await connection.scalar(
                text("SELECT pg_try_advisory_lock(:key)"), {"key": ADVISORY_LOCK_KEY}
            )
        except Exception:
            await connection.close()
            raise

        if not acquired:
            await connection.close()
            raise AlreadyRunningError("PostgreSQL advisory lock is held")

        # The lock lives on this connection, so it must stay open for the
        # lifetime of the process — returning it to the pool would release it.
        self._connection = connection
        logger.debug("Acquired PostgreSQL advisory lock %s", ADVISORY_LOCK_KEY)

    async def release(self) -> None:
        """Drop the lock and close the connection."""
        if self._connection is None:
            return
        with contextlib.suppress(Exception):
            await self._connection.execute(
                text("SELECT pg_advisory_unlock(:key)"), {"key": ADVISORY_LOCK_KEY}
            )
        with contextlib.suppress(Exception):
            await self._connection.close()
        self._connection = None


class _FileLock:
    """Exclusive OS lock on a sidecar file, for SQLite and development."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._handle: Any = None

    def acquire(self) -> None:
        """Take the lock, or raise if somebody else has it."""
        self._path.parent.mkdir(parents=True, exist_ok=True)

        handle = None
        try:
            # The open is inside the try because Windows refuses it outright
            # when another process holds the range — the sharing violation
            # arrives here, not at the locking call.
            handle = self._path.open("a+b")

            # Lock byte zero specifically. msvcrt.locking takes the range from
            # the *current* file position, and "a+b" starts at end-of-file, so
            # without this each process would lock a different byte and none
            # would ever see a conflict.
            handle.seek(0)

            if sys.platform == "win32":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            if handle is not None:
                handle.close()
            raise AlreadyRunningError(f"{self._path.name} is locked") from None

        self._handle = handle
        logger.debug("Acquired file lock %s (pid %d)", self._path, os.getpid())

    def release(self) -> None:
        """Release the lock and close the file."""
        if self._handle is None:
            return
        with contextlib.suppress(Exception):
            if sys.platform == "win32":
                import msvcrt

                self._handle.seek(0)
                msvcrt.locking(self._handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        with contextlib.suppress(Exception):
            self._handle.close()
        self._handle = None


@contextlib.asynccontextmanager
async def single_instance(engine: AsyncEngine, *, lock_dir: Path) -> AsyncIterator[None]:
    """Hold a start-up lock for the lifetime of the block.

    Args:
        engine: The application's engine; its dialect selects the mechanism.
        lock_dir: Where to place the lock file on non-PostgreSQL backends.

    Yields:
        Nothing — the lock is held for the duration.

    Raises:
        AlreadyRunningError: Another instance is running.
    """
    if engine.dialect.name == "postgresql":
        postgres = _PostgresLock(engine)
        await postgres.acquire()
        try:
            yield
        finally:
            await postgres.release()
        return

    file_lock = _FileLock(lock_dir / "bot.lock")
    file_lock.acquire()
    try:
        yield
    finally:
        file_lock.release()
