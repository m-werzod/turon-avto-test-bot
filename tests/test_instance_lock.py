"""The start-up lock that stops a second copy polling the same token.

Telegram permits one getUpdates poller per token. A second copy does not fail
loudly — both take turns being kicked off and the bot answers about half the
time — so the second one has to be stopped here instead.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from bot.utils.instance_lock import AlreadyRunningError, single_instance


@pytest.fixture
def engine(tmp_path: Path):  # type: ignore[no-untyped-def]
    """A SQLite engine, which selects the file-lock mechanism."""
    return create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'x.db'}")


class TestFileLock:
    """SQLite and development installs."""

    async def test_the_first_holder_gets_the_lock(self, engine, tmp_path: Path) -> None:
        async with single_instance(engine, lock_dir=tmp_path):
            assert (tmp_path / "bot.lock").exists()

    async def test_a_second_holder_is_refused(self, engine, tmp_path: Path) -> None:
        async with single_instance(engine, lock_dir=tmp_path):
            with pytest.raises(AlreadyRunningError):
                async with single_instance(engine, lock_dir=tmp_path):
                    pass

    async def test_the_lock_is_reusable_after_release(self, engine, tmp_path: Path) -> None:
        """A clean shutdown must not leave the next start blocked."""
        async with single_instance(engine, lock_dir=tmp_path):
            pass

        async with single_instance(engine, lock_dir=tmp_path):
            pass  # would raise if the first hold had not been released

    async def test_the_lock_survives_an_exception_inside_the_block(
        self, engine, tmp_path: Path
    ) -> None:
        """A crash must release it too, or the bot never restarts."""
        with pytest.raises(ValueError):
            async with single_instance(engine, lock_dir=tmp_path):
                raise ValueError("boom")

        async with single_instance(engine, lock_dir=tmp_path):
            pass

    async def test_the_error_names_how_to_fix_it(self, engine, tmp_path: Path) -> None:
        """Whoever hits this is mid-deploy and wants the command, not a class name."""
        async with single_instance(engine, lock_dir=tmp_path):
            with pytest.raises(AlreadyRunningError) as caught:
                async with single_instance(engine, lock_dir=tmp_path):
                    pass

        message = str(caught.value)
        assert "supervisorctl stop" in message
        assert "systemctl stop" in message
        assert "one poller per token" in message

    async def test_the_lock_directory_is_created_if_missing(self, engine, tmp_path: Path) -> None:
        """A fresh deployment has no logs/ yet."""
        target = tmp_path / "not" / "there" / "yet"

        async with single_instance(engine, lock_dir=target):
            assert (target / "bot.lock").exists()


class TestHeartbeat:
    """The liveness signal the deployment watchdog restarts on.

    Untested, this would be the worst kind of failure: the watchdog would restart
    a perfectly healthy bot every ten minutes because the file it watches was
    never written.
    """

    @staticmethod
    def _context(timezone):  # type: ignore[no-untyped-def]
        """A JobContext carrying only what write_heartbeat touches."""
        from bot.scheduler.jobs import JobContext

        return JobContext(
            db=None,  # type: ignore[arg-type]
            quiz=None,  # type: ignore[arg-type]
            notify=None,  # type: ignore[arg-type]
            timezone=timezone,
        )

    async def test_it_writes_a_timestamp(self, tmp_path: Path) -> None:
        from zoneinfo import ZoneInfo

        from bot.scheduler.jobs import write_heartbeat

        target = tmp_path / "heartbeat"
        await write_heartbeat(self._context(ZoneInfo("Asia/Tashkent")), path=target)

        assert target.exists()
        # Parseable, and in the configured zone rather than the server's.
        from datetime import datetime

        stamp = datetime.fromisoformat(target.read_text(encoding="utf-8"))
        assert stamp.tzinfo is not None
        assert stamp.utcoffset().total_seconds() == 5 * 3600  # type: ignore[union-attr]

    async def test_it_creates_the_directory(self, tmp_path: Path) -> None:
        """A fresh deployment has no logs/ before the first beat."""
        from zoneinfo import ZoneInfo

        from bot.scheduler.jobs import write_heartbeat

        target = tmp_path / "logs" / "heartbeat"
        await write_heartbeat(self._context(ZoneInfo("UTC")), path=target)

        assert target.exists()

    async def test_an_unwritable_path_does_not_raise(self, tmp_path: Path) -> None:
        """Losing the heartbeat costs monitoring; it must not cost posting."""
        from zoneinfo import ZoneInfo

        from bot.scheduler.jobs import write_heartbeat

        # A directory where the file should be: writing it can only fail.
        target = tmp_path / "blocked"
        target.mkdir()

        await write_heartbeat(self._context(ZoneInfo("UTC")), path=target)

    async def test_the_beat_advances(self, tmp_path: Path) -> None:
        """A frozen timestamp is exactly what the watchdog looks for."""
        import asyncio
        from zoneinfo import ZoneInfo

        from bot.scheduler.jobs import write_heartbeat

        target = tmp_path / "heartbeat"
        context = self._context(ZoneInfo("UTC"))

        await write_heartbeat(context, path=target)
        first = target.read_text(encoding="utf-8")
        await asyncio.sleep(0.01)
        await write_heartbeat(context, path=target)

        assert target.read_text(encoding="utf-8") != first
