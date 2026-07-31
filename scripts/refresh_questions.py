"""Refresh the shared question bank from the web sources, headlessly.

    python scripts/refresh_questions.py
    python scripts/refresh_questions.py --source at --no-images

Same importers the panel's "Testlarni yangilash" button uses, without needing a
Telegram round-trip — which is what makes it usable from cron. The upsert is
idempotent, so re-running adds only what is new.

Exits non-zero if every source failed, so cron surfaces a broken run instead of
silently doing nothing for weeks.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot.config.settings import load_settings_or_exit
from bot.database.session import Database
from bot.services.import_service import ImportService
from bot.services.media_service import MediaService
from bot.sources.base import SourceError
from bot.sources.registry import WEB_SOURCES, build_web_source
from bot.utils.logging import get_logger, setup_logging

logger = get_logger("refresh_questions")


async def refresh(keys: list[str], *, language: str, download_images: bool) -> int:
    """Import from each named source.

    Args:
        keys: Source tokens from :data:`WEB_SOURCES`.
        language: Question language to import.
        download_images: Fetch and cache referenced images.

    Returns:
        A process exit code: 0 if any source succeeded, 1 if all failed.
    """
    settings = load_settings_or_exit()
    database = Database(settings.database_url)
    media = MediaService(
        settings.media_root,
        max_retries=settings.max_retries,
        retry_backoff=settings.retry_backoff_seconds,
    )
    importer = ImportService(media)

    succeeded = 0
    total_created = 0

    try:
        for key in keys:
            label = WEB_SOURCES[key][0]
            logger.info("Importing from %s", label)

            try:
                async with database.session() as session:
                    report = await importer.import_source(
                        session,
                        build_web_source(key, language=language),
                        download_images=download_images,
                    )
            except SourceError as exc:
                # One unreachable site must not abort the others: a weekly cron
                # that gives up on the first failure quietly stops refreshing.
                logger.error("%s failed: %s", label, exc)
                continue
            except Exception:
                logger.exception("%s failed unexpectedly", label)
                continue

            succeeded += 1
            total_created += report.result.created
            logger.info(
                "%s: %d new, %d updated, %d unchanged, %d images, bank now %d",
                label,
                report.result.created,
                report.result.updated,
                report.result.unchanged,
                report.images_downloaded,
                report.total_in_bank,
            )
    finally:
        await database.dispose()

    if succeeded == 0:
        logger.error("Every source failed; the bank is unchanged")
        return 1

    logger.info("Refresh complete: %d source(s) ok, %d new question(s)", succeeded, total_created)
    return 0


def main() -> None:
    """Parse arguments and run."""
    parser = argparse.ArgumentParser(description="Refresh the shared question bank.")
    parser.add_argument(
        "--source",
        action="append",
        choices=sorted(WEB_SOURCES),
        help="Import from one source only. Repeatable. Defaults to all of them.",
    )
    parser.add_argument(
        "--language",
        default=None,
        help="Question language (uz or ru). Defaults to DEFAULT_LANGUAGE.",
    )
    parser.add_argument(
        "--no-images",
        action="store_true",
        help="Skip downloading images. Much faster; questions post without pictures.",
    )
    args = parser.parse_args()

    settings = load_settings_or_exit()
    setup_logging(
        level=settings.log_level,
        log_dir=settings.log_dir,
        max_bytes=settings.log_max_bytes,
        backup_count=settings.log_backup_count,
        log_format=settings.log_format,
    )

    raise SystemExit(
        asyncio.run(
            refresh(
                args.source or sorted(WEB_SOURCES),
                language=args.language or settings.default_language,
                download_images=not args.no_images,
            )
        )
    )


if __name__ == "__main__":
    main()
