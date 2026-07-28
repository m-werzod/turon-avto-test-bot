"""Pre-flight check: everything that must be true before the bot can post.

    python scripts/doctor.py

Each check reports OK, WARN or FAIL with the exact fix. Run it whenever the bot
will not start, or when it starts but nothing appears in the channel — the usual
cause is one of the last four checks (no bank, no channel, no schedule, paused)
rather than anything broken.

Exits non-zero if any check FAILs, so it doubles as a deployment gate.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot.config.settings import PROJECT_ROOT, Settings

OK = "OK  "
WARN = "WARN"
FAIL = "FAIL"

_failures = 0
_warnings = 0


def report(status: str, title: str, detail: str = "", fix: str = "") -> None:
    """Print one check result."""
    global _failures, _warnings
    if status == FAIL:
        _failures += 1
    elif status == WARN:
        _warnings += 1

    print(f"  [{status}] {title}")
    if detail:
        print(f"         {detail}")
    if fix and status != OK:
        for line in fix.splitlines():
            print(f"         -> {line}")


def check_configuration() -> bool:
    """Required values are present, from wherever configuration actually comes.

    Deliberately checks the *effective* values rather than the text of ``.env``.
    Under Docker, Railway and Render the settings arrive as environment
    variables and there is frequently no ``.env`` file at all — reading the file
    alone would report a failure on every one of those deployments.
    """
    print("\nConfiguration")
    env_path = PROJECT_ROOT / ".env"
    env_exists = env_path.exists()

    sources = []
    if env_exists:
        sources.append(".env")
    if any(os.environ.get(name) for name in ("BOT_TOKEN", "ADMIN_IDS", "DATABASE_URL")):
        sources.append("environment")

    if not sources:
        report(
            FAIL,
            "configuration source",
            "no .env file and no environment variables",
            "cp .env.example .env    then fill in BOT_TOKEN and ADMIN_IDS",
        )
        return False

    report(OK, "configuration source", " + ".join(sources))

    # Placeholders are only a problem if they survive into the effective value.
    unfilled = [
        name
        for name in ("BOT_TOKEN", "ADMIN_IDS")
        if "PASTE_YOUR" in _effective(name, env_path if env_exists else None)
    ]
    if unfilled:
        report(
            FAIL,
            "required values",
            f"still placeholders: {', '.join(unfilled)}",
            f"edit {env_path}\n"
            "BOT_TOKEN  -> from @BotFather\n"
            "ADMIN_IDS  -> your numeric id from @userinfobot",
        )
        return False

    report(OK, "required values", "BOT_TOKEN and ADMIN_IDS are set")
    return True


def _effective(name: str, env_path: Path | None) -> str:
    """Return the value that will actually be used for ``name``.

    Mirrors pydantic-settings precedence: a real environment variable wins over
    the ``.env`` file.
    """
    from_environ = os.environ.get(name)
    if from_environ:
        return from_environ

    if env_path is None:
        return ""

    for line in env_path.read_text(encoding="utf-8", errors="replace").splitlines():
        stripped = line.strip()
        if stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        if key.strip() == name:
            return value.strip()
    return ""


def check_settings() -> Settings | None:
    """Settings load and validate."""
    try:
        settings = Settings()  # type: ignore[call-arg]
    except Exception as exc:  # noqa: BLE001 - report any validation failure
        first = str(exc).splitlines()
        detail = next((line.strip() for line in first if "Value error" in line), str(exc)[:200])
        report(FAIL, "settings validation", detail, "fix the named value in .env")
        return None

    try:
        admins = settings.admin_ids
    except ValueError as exc:
        report(FAIL, "ADMIN_IDS", str(exc), "use the numeric id from @userinfobot, not @username")
        return None

    if not admins:
        report(FAIL, "ADMIN_IDS", "empty", "set ADMIN_IDS=<your numeric id>")
        return None

    report(OK, "settings validation", f"{len(admins)} admin(s), timezone {settings.timezone_name}")
    return settings


def check_directories(settings: Settings) -> None:
    """Writable directories for logs, media and backups."""
    print("\nFilesystem")
    for label, path in (
        ("logs", settings.log_dir),
        ("media", settings.media_root),
        ("backups", settings.backup_dir),
    ):
        try:
            path.mkdir(parents=True, exist_ok=True)
            probe = path / ".write_probe"
            probe.write_text("x", encoding="utf-8")
            probe.unlink()
            report(OK, f"{label} directory", str(path))
        except OSError as exc:
            report(FAIL, f"{label} directory", f"{path}: {exc}", "check ownership and permissions")


async def check_telegram(settings: Settings) -> bool:
    """The token authenticates against Telegram."""
    print("\nTelegram")
    from aiogram import Bot
    from aiogram.exceptions import TelegramAPIError

    bot = Bot(token=settings.bot_token)
    try:
        me = await bot.get_me()
    except TelegramAPIError as exc:
        report(
            FAIL,
            "bot token",
            str(exc),
            "the token is wrong or was revoked\nget a fresh one from @BotFather (/mytoken)",
        )
        return False
    except Exception as exc:  # noqa: BLE001 - network problems land here
        report(FAIL, "bot token", f"could not reach Telegram: {exc}", "check internet access")
        return False
    finally:
        await bot.session.close()

    report(OK, "bot token", f"@{me.username} (id {me.id})")
    return True


async def check_database(settings: Settings) -> bool:
    """The database is reachable and migrated."""
    print("\nDatabase")
    from sqlalchemy import inspect, text

    from bot.database.session import Database

    backend = settings.database_url.split("://", 1)[0]
    database = Database(settings.database_url)

    try:
        async with database.engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001 - any connection failure is a FAIL
        message = str(exc).splitlines()[0][:160]
        report(
            FAIL,
            "connection",
            f"{backend}: {message}",
            "Docker:    docker compose up -d postgres\n"
            "No Docker: set DATABASE_URL=sqlite+aiosqlite:///./turon.db in .env",
        )
        await database.dispose()
        return False

    report(OK, "connection", backend)

    try:
        async with database.engine.connect() as connection:
            tables = await connection.run_sync(lambda sync: inspect(sync).get_table_names())
    except Exception as exc:  # noqa: BLE001
        report(WARN, "schema", f"could not inspect: {exc}")
        await database.dispose()
        return True

    expected = {"questions", "channels", "cycles", "quiz_posts", "deliveries", "schedule_slots"}
    missing = expected - set(tables)
    if missing:
        report(
            FAIL,
            "migrations",
            f"missing tables: {', '.join(sorted(missing))}",
            "alembic upgrade head",
        )
        await database.dispose()
        return False

    report(OK, "migrations", f"{len(tables)} tables present")
    await database.dispose()
    return True


async def check_readiness(settings: Settings) -> None:
    """Content, channels and schedule — why a running bot posts nothing."""
    print("\nReadiness to post")
    from bot.database.repositories import (
        QuestionRepository,
    )
    from bot.database.session import Database

    database = Database(settings.database_url)
    try:
        async with database.session() as session:
            # --- shared by every user -----------------------------------------
            total = await QuestionRepository(session).count_active()
            if total == 0:
                report(
                    FAIL,
                    "question bank",
                    "empty",
                    'panel -> "Testlarni yangilash" (operator only)\n'
                    "a 20-question sample ships in data/sample_questions.json",
                )
            else:
                with_images = await QuestionRepository(session).count_with_images()
                report(OK, "question bank", f"{total} active ({with_images} with images)")

            # --- per user ------------------------------------------------------
            owners = await _known_owners(session)
            if not owners:
                report(
                    WARN,
                    "users",
                    "nobody has connected a channel yet",
                    "open the bot in Telegram and press /start",
                )
                return

            report(OK, "users", f"{len(owners)} with a channel or a schedule")

            for owner_id in owners:
                await _report_owner(session, owner_id, settings)

    except Exception as exc:  # noqa: BLE001 - covered by the database check above
        report(WARN, "readiness", f"could not read state: {str(exc)[:120]}")
    finally:
        await database.dispose()


async def _known_owners(session: object) -> list[int]:
    """Everyone who has connected a channel or set a schedule.

    Deliberately spans owners: the doctor's job is to describe the whole
    installation, which is the one place a cross-tenant read is the point rather
    than a leak.
    """
    from sqlalchemy import select

    from bot.database.models.channel import Channel
    from bot.database.models.schedule import ScheduleSlot

    found: set[int] = set()
    for column in (Channel.owner_id, ScheduleSlot.owner_id):
        result = await session.scalars(select(column).distinct())  # type: ignore[attr-defined]
        found.update(result)
    return sorted(found)


async def _report_owner(session: object, owner_id: int, settings: Settings) -> None:
    """Print one user's readiness to post."""
    from bot.database.repositories import (
        ChannelRepository,
        CycleRepository,
        ScheduleRepository,
        SettingsRepository,
    )

    channels = await ChannelRepository(session, owner_id).list_active()  # type: ignore[arg-type]
    slots = await ScheduleRepository(session, owner_id).list_enabled()  # type: ignore[arg-type]
    paused = await SettingsRepository(session, owner_id).is_scheduler_paused()  # type: ignore[arg-type]
    per_send = await SettingsRepository(session, owner_id).questions_per_send()  # type: ignore[arg-type]

    print(f"\n  user {owner_id}")

    if channels:
        report(OK, "  channels", ", ".join(channel.display_name for channel in channels))
    else:
        report(
            WARN,
            "  channels",
            "none connected",
            'panel -> "Kanalni ulash"; add the bot to the channel as administrator first',
        )

    if slots:
        times = ", ".join(slot.label for slot in slots)
        report(
            OK,
            "  schedule",
            f"{times} ({settings.timezone_name}), {per_send} question(s) each",
        )
    else:
        report(WARN, "  schedule", "no posting times set", 'panel -> "Jadval"')

    if paused:
        report(WARN, "  scheduler", "PAUSED for this user", 'panel -> "Davom ettirish"')
    else:
        report(OK, "  scheduler", "running")

    cycle = await CycleRepository(session, owner_id).get_open_cycle()  # type: ignore[arg-type]
    if cycle is not None:
        cycles = CycleRepository(session, owner_id)  # type: ignore[arg-type]
        used = await cycles.count_posts_in_cycle(cycle.id)
        left = await cycles.count_remaining(cycle.id)
        report(OK, "  cycle", f"#{cycle.number} — {used} posted, {left} remaining")


async def main() -> int:
    """Run every check and summarise."""
    print("=" * 62)
    print("  Turon Avto Test | UZ — pre-flight check")
    print("=" * 62)

    if not check_configuration():
        _summary()
        return 1

    settings = check_settings()
    if settings is None:
        _summary()
        return 1

    check_directories(settings)

    telegram_ok = await check_telegram(settings)
    database_ok = await check_database(settings)

    if database_ok:
        await check_readiness(settings)

    _summary()
    if _failures:
        return 1
    if not telegram_ok:
        return 1
    return 0


def _summary() -> None:
    """Print the closing verdict."""
    print("\n" + "=" * 62)
    if _failures:
        print(f"  {_failures} problem(s) must be fixed before the bot can run.")
    elif _warnings:
        print(f"  Ready to start, with {_warnings} warning(s) above.")
    else:
        print("  All checks passed. Start with:  python -m bot")
    print("=" * 62)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
