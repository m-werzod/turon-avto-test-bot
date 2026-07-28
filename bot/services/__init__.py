"""Business logic.

Services own the behaviour that spans more than one repository. Handlers stay
thin translation layers between Telegram and these, and scheduler jobs call the
exact same code paths the admin buttons do — so "Send now" and the 08:00 job can
never drift apart.
"""

from bot.services.backup_service import BackupResult, BackupService
from bot.services.branding_service import BrandingService
from bot.services.channel_service import ChannelCheckError, ChannelInfo, ChannelService
from bot.services.import_service import ImportReport, ImportService
from bot.services.media_service import MediaError, MediaResult, MediaService
from bot.services.notify_service import NotifyService
from bot.services.quiz_service import (
    ChannelOutcome,
    NoChannelsError,
    QuizService,
    SendReport,
)
from bot.services.stats_service import Statistics, StatsService

__all__ = [
    "BackupResult",
    "BackupService",
    "BrandingService",
    "ChannelCheckError",
    "ChannelInfo",
    "ChannelOutcome",
    "ChannelService",
    "ImportReport",
    "ImportService",
    "MediaError",
    "MediaResult",
    "MediaService",
    "NoChannelsError",
    "NotifyService",
    "QuizService",
    "SendReport",
    "Statistics",
    "StatsService",
]
