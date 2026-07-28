"""Database and configuration export.

A backup is a single ZIP containing JSON dumps of every table plus a manifest.
JSON rather than ``pg_dump`` on purpose: the archive stays readable, restorable
into any PostgreSQL version, and inspectable without a database at all — and the
bot container has no ``pg_dump`` binary in it.
"""

from __future__ import annotations

import json
import zipfile
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.database.models import (
    BotUser,
    Channel,
    Cycle,
    Delivery,
    EventLog,
    Question,
    QuizPost,
    ScheduleSlot,
    Setting,
)
from bot.database.models.event_log import EventType
from bot.database.repositories import EventRepository
from bot.utils.logging import get_logger

logger = get_logger(__name__)

#: Telegram refuses bot uploads above 50 MB.
TELEGRAM_UPLOAD_LIMIT = 50 * 1024 * 1024

#: Tables included in a backup, in dependency order so a restore can replay them
#: top to bottom without tripping a foreign key.
_EXPORT_MODELS = (
    ("questions", Question),
    ("channels", Channel),
    ("cycles", Cycle),
    ("quiz_posts", QuizPost),
    ("deliveries", Delivery),
    ("schedule_slots", ScheduleSlot),
    ("settings", Setting),
    ("bot_users", BotUser),
    ("event_logs", EventLog),
)


@dataclass(slots=True)
class BackupResult:
    """A completed backup."""

    path: Path
    size_bytes: int
    row_counts: dict[str, int]

    @property
    def fits_telegram(self) -> bool:
        """Whether the archive can be sent through the Bot API."""
        return self.size_bytes <= TELEGRAM_UPLOAD_LIMIT

    @property
    def human_size(self) -> str:
        """Size rendered for an admin message."""
        size = float(self.size_bytes)
        for unit in ("B", "KB", "MB", "GB"):
            if size < 1024 or unit == "GB":
                return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
            size /= 1024
        return f"{size:.1f} GB"


def _json_default(value: Any) -> Any:
    """Serialise types the stdlib JSON encoder does not handle."""
    if isinstance(value, datetime | date | time):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, set | frozenset):
        return sorted(value)
    return str(value)


def _row_to_dict(instance: Any) -> dict[str, Any]:
    """Convert an ORM instance into a plain serialisable mapping."""
    return {
        column.name: getattr(instance, column.name)
        for column in instance.__table__.columns
    }


class BackupService:
    """Creates backup archives."""

    def __init__(self, backup_dir: Path, media_root: Path | None = None) -> None:
        """Args:
        backup_dir: Where archives are written.
        media_root: Image cache, used only for the manifest's file count.
        """
        self.backup_dir = backup_dir
        self.media_root = media_root
        self.backup_dir.mkdir(parents=True, exist_ok=True)

    async def create(
        self, session: AsyncSession, *, include_event_logs: bool = False
    ) -> BackupResult:
        """Export the database into a timestamped ZIP.

        Args:
            session: Open session.
            include_event_logs: Include the audit trail. Off by default — it is
                the largest and least valuable table to restore, and skipping it
                usually keeps the archive under Telegram's upload limit.

        Returns:
            The archive that was written.
        """
        stamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
        archive_path = self.backup_dir / f"turon_backup_{stamp}.zip"
        row_counts: dict[str, int] = {}

        with zipfile.ZipFile(
            archive_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6
        ) as archive:
            for table_name, model in _EXPORT_MODELS:
                if table_name == "event_logs" and not include_event_logs:
                    continue

                rows = list(await session.scalars(select(model)))
                payload = [_row_to_dict(row) for row in rows]
                row_counts[table_name] = len(payload)

                archive.writestr(
                    f"data/{table_name}.json",
                    json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default),
                )

            media_files = 0
            if self.media_root and self.media_root.exists():
                media_files = sum(1 for path in self.media_root.rglob("*") if path.is_file())

            manifest = {
                "created_at": datetime.now(UTC).isoformat(),
                "format_version": 1,
                "tables": row_counts,
                "media_files_on_disk": media_files,
                "includes_event_logs": include_event_logs,
                "note": (
                    "Images are not bundled. They are re-downloadable from the "
                    "image_url stored on each question, and bundling them would "
                    "push the archive past Telegram's 50 MB upload limit."
                ),
            }
            archive.writestr(
                "manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2)
            )

        size = archive_path.stat().st_size
        await EventRepository(session).record(
            EventType.BACKUP_CREATED,
            f"Backup created: {archive_path.name} ({size / 1024:.0f} KB)",
            payload={"file": archive_path.name, "bytes": size, "tables": row_counts},
        )
        logger.info("Backup written to %s (%d bytes)", archive_path, size)

        return BackupResult(path=archive_path, size_bytes=size, row_counts=row_counts)

    def prune(self, keep: int = 10) -> int:
        """Delete all but the newest ``keep`` archives.

        Returns:
            Number of files removed.
        """
        archives = sorted(
            self.backup_dir.glob("turon_backup_*.zip"),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        removed = 0
        for stale in archives[keep:]:
            try:
                stale.unlink()
                removed += 1
            except OSError as exc:  # pragma: no cover - filesystem dependent
                logger.warning("Could not delete old backup %s: %s", stale, exc)
        if removed:
            logger.info("Pruned %d old backup(s)", removed)
        return removed
