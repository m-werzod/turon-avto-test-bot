"""ORM models.

Every model is re-exported here so that ``import bot.database.models`` registers
the whole schema with the declarative metadata. Alembic's autogenerate relies on
that: a model that is never imported is invisible to it and silently absent from
migrations.
"""

from bot.database.base import Base
from bot.database.models.channel import Channel
from bot.database.models.cycle import Cycle
from bot.database.models.delivery import Delivery, DeliveryStatus
from bot.database.models.event_log import EventLog, EventType
from bot.database.models.question import Question
from bot.database.models.quiz_post import PostTrigger, QuizPost
from bot.database.models.schedule import ScheduleSlot
from bot.database.models.setting import Setting, SettingKey
from bot.database.models.user import BotUser

__all__ = [
    "Base",
    "BotUser",
    "Channel",
    "Cycle",
    "Delivery",
    "DeliveryStatus",
    "EventLog",
    "EventType",
    "PostTrigger",
    "Question",
    "QuizPost",
    "ScheduleSlot",
    "Setting",
    "SettingKey",
]
