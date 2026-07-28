"""Process entrypoint.

Run with ``python -m bot`` or via the ``turon-bot`` console script.
"""

from __future__ import annotations

import asyncio
import sys

from bot.config.settings import load_settings_or_exit
from bot.utils.logging import get_logger, setup_logging

logger = get_logger(__name__)


def run() -> None:
    """Configure logging, load settings and start the bot."""
    settings = load_settings_or_exit()

    setup_logging(
        level=settings.log_level,
        log_dir=settings.log_dir,
        max_bytes=settings.log_max_bytes,
        backup_count=settings.log_backup_count,
        log_format=settings.log_format,
    )

    logger.info("Starting Turon Avto Test bot")

    # Imported after logging is configured so start-up messages are formatted and
    # written to the log files like everything else.
    from bot.app import run_app

    try:
        asyncio.run(run_app(settings))
    except KeyboardInterrupt:
        logger.info("Interrupted by user")
    except Exception:
        logger.critical("Fatal error, the bot is exiting", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    run()
