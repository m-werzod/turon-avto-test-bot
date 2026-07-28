"""Repositories: every database query the application makes lives here.

Services and handlers depend on these rather than on SQLAlchemy directly, which
keeps query construction in one testable place and leaves the rest of the code
free of session plumbing.
"""

from bot.database.repositories.base import BaseRepository
from bot.database.repositories.channel_repo import (
    ChannelAlreadyConnectedError,
    ChannelRepository,
)
from bot.database.repositories.cycle_repo import CycleExhaustedError, CycleRepository
from bot.database.repositories.delivery_repo import DeliveryRepository
from bot.database.repositories.event_repo import EventRepository
from bot.database.repositories.question_repo import QuestionRepository, UpsertResult
from bot.database.repositories.schedule_repo import ScheduleRepository
from bot.database.repositories.settings_repo import SettingsRepository
from bot.database.repositories.user_repo import UserRepository

__all__ = [
    "BaseRepository",
    "ChannelAlreadyConnectedError",
    "ChannelRepository",
    "CycleExhaustedError",
    "CycleRepository",
    "DeliveryRepository",
    "EventRepository",
    "QuestionRepository",
    "ScheduleRepository",
    "SettingsRepository",
    "UpsertResult",
    "UserRepository",
]
