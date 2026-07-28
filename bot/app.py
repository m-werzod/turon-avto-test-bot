"""Application composition and lifecycle.

Everything is constructed here and injected downwards. Handlers receive their
services through the dispatcher's workflow data rather than importing globals,
which is what lets the test suite build the same objects against a throwaway
database.
"""

from __future__ import annotations

import asyncio
import contextlib
import signal

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage

from bot.config.settings import Settings
from bot.database.models.event_log import EventType
from bot.database.repositories import EventRepository
from bot.database.session import Database
from bot.handlers import build_root_router
from bot.middlewares import (
    DatabaseMiddleware,
    LoggingMiddleware,
    ThrottlingMiddleware,
    UserContextMiddleware,
)
from bot.scheduler.jobs import JobContext
from bot.scheduler.scheduler import QuizScheduler
from bot.services import (
    BackupService,
    BrandingService,
    ChannelService,
    ImportService,
    MediaService,
    NotifyService,
    QuizService,
    StatsService,
)
from bot.utils.logging import get_logger

logger = get_logger(__name__)


class Application:
    """Owns every long-lived object and the start/stop sequence."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

        self.bot = Bot(
            token=settings.bot_token,
            default=DefaultBotProperties(parse_mode=ParseMode.HTML),
        )
        self.dispatcher = Dispatcher(storage=MemoryStorage())
        self.database = Database(settings.database_url)

        self.media = MediaService(
            settings.media_root,
            max_retries=settings.max_retries,
            retry_backoff=settings.retry_backoff_seconds,
        )
        self.notify = NotifyService(
            self.bot,
            settings.admin_ids,
            enabled=settings.notify_admins_on_error,
            timezone=settings.timezone,
        )
        self.quiz = QuizService(
            self.bot,
            self.media,
            send_as_document=settings.send_images_as_document,
            content_language_default=settings.default_language,
        )
        self.branding = BrandingService(self.bot, settings.brand_logo)
        self.imports = ImportService(self.media)
        self.channels = ChannelService(self.bot)
        self.stats = StatsService(timezone=settings.timezone)
        self.backups = BackupService(settings.backup_dir, media_root=settings.media_root)

        self.scheduler = QuizScheduler(
            JobContext(
                db=self.database,
                quiz=self.quiz,
                notify=self.notify,
                timezone=settings.timezone,
                admin_language=settings.default_language,
                backup_service=self.backups,
            ),
            timezone=settings.timezone,
            misfire_grace=settings.scheduler_misfire_grace,
        )

        self._configure_dispatcher()

    def _configure_dispatcher(self) -> None:
        """Register middlewares, routers and injected dependencies."""
        # Outer middlewares run before filters, so a session and the resolved
        # language are available to everything, including the admin gate.
        self.dispatcher.update.outer_middleware(DatabaseMiddleware(self.database.session_factory))
        self.dispatcher.update.outer_middleware(
            UserContextMiddleware(
                self.settings.admin_ids, default_language=self.settings.default_language
            )
        )
        self.dispatcher.message.middleware(LoggingMiddleware())
        self.dispatcher.callback_query.middleware(LoggingMiddleware())
        self.dispatcher.message.middleware(ThrottlingMiddleware())
        self.dispatcher.callback_query.middleware(ThrottlingMiddleware())

        self.dispatcher.include_router(build_root_router(self.settings.admin_ids))

        # Handlers declare these as parameters and aiogram injects by name.
        self.dispatcher.workflow_data.update(
            {
                "settings": self.settings,
                "db": self.database,
                "scheduler": self.scheduler,
                "quiz_service": self.quiz,
                "import_service": self.imports,
                "channel_service": self.channels,
                "stats_service": self.stats,
                "backup_service": self.backups,
                "notify": self.notify,
                "media": self.media,
                "branding": self.branding,
                "timezone": self.settings.timezone,
                "log_dir": self.settings.log_dir,
                "send_as_document": self.settings.send_images_as_document,
            }
        )

    async def _startup(self) -> None:
        """Verify connectivity, start the scheduler and announce readiness."""
        if not await self.database.healthcheck():
            raise RuntimeError(
                "Cannot reach the database. Check DATABASE_URL and that migrations "
                "have been applied (alembic upgrade head)."
            )

        me = await self.bot.get_me()
        logger.info("Authorized as @%s (id=%s)", me.username, me.id)

        await self.scheduler.start()

        async with self.database.session() as session:
            await EventRepository(session).record(
                EventType.BOT_STARTED,
                f"Bot @{me.username} started",
                payload={"username": me.username, "timezone": str(self.settings.timezone)},
            )

        logger.info(
            "Startup complete — %d admin(s), timezone %s",
            len(self.settings.admin_ids),
            self.settings.timezone,
        )

    async def _shutdown(self) -> None:
        """Stop everything in reverse order of construction."""
        logger.info("Shutting down…")

        with contextlib.suppress(Exception):
            await self.scheduler.shutdown()

        with contextlib.suppress(Exception):
            async with self.database.session() as session:
                await EventRepository(session).record(EventType.BOT_STOPPED, "Bot stopped")

        with contextlib.suppress(Exception):
            await self.bot.session.close()

        with contextlib.suppress(Exception):
            await self.database.dispose()

        logger.info("Shutdown complete")

    async def run(self) -> None:
        """Start the bot and poll until interrupted.

        Drops any update queued while the bot was offline: replaying a backlog of
        stale button presses after a restart would re-run actions the admin has
        long since moved past.
        """
        stop_event = asyncio.Event()
        self._install_signal_handlers(stop_event)

        await self._startup()

        polling = asyncio.create_task(
            self.dispatcher.start_polling(
                self.bot,
                allowed_updates=self.dispatcher.resolve_used_update_types(),
                handle_signals=False,  # handled here so shutdown is orderly
                notify=self.notify,
                drop_pending_updates=True,
            ),
            name="polling",
        )
        waiter = asyncio.create_task(stop_event.wait(), name="stop-waiter")

        try:
            done, pending = await asyncio.wait(
                {polling, waiter}, return_when=asyncio.FIRST_COMPLETED
            )
            for task in pending:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            # Re-raise a polling failure rather than exiting silently.
            for task in done:
                if task is polling and not task.cancelled():
                    task.result()
        finally:
            with contextlib.suppress(Exception):
                await self.dispatcher.stop_polling()
            await self._shutdown()

    @staticmethod
    def _install_signal_handlers(stop_event: asyncio.Event) -> None:
        """Trip ``stop_event`` on SIGINT/SIGTERM where the platform allows it.

        Windows has no loop-level signal handlers; there KeyboardInterrupt from
        the default handler is what stops the process.
        """
        loop = asyncio.get_running_loop()
        for signal_name in ("SIGINT", "SIGTERM"):
            sig = getattr(signal, signal_name, None)
            if sig is None:
                continue
            try:
                loop.add_signal_handler(sig, stop_event.set)
            except NotImplementedError:  # pragma: no cover - Windows
                logger.debug("Signal handler for %s is unavailable here", signal_name)


async def run_app(settings: Settings) -> None:
    """Build and run the application.

    Args:
        settings: Validated configuration.
    """
    app = Application(settings)
    await app.run()
