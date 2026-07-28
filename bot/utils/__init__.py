"""Cross-cutting helpers shared by every layer of the bot."""

from bot.utils.logging import get_logger, setup_logging
from bot.utils.retry import RetryError, retry_async
from bot.utils.text import (
    CAPTION_LIMIT,
    MESSAGE_LIMIT,
    POLL_EXPLANATION_LIMIT,
    POLL_OPTION_LIMIT,
    POLL_QUESTION_LIMIT,
    collapse_whitespace,
    truncate,
)

__all__ = [
    "CAPTION_LIMIT",
    "MESSAGE_LIMIT",
    "POLL_EXPLANATION_LIMIT",
    "POLL_OPTION_LIMIT",
    "POLL_QUESTION_LIMIT",
    "RetryError",
    "collapse_whitespace",
    "get_logger",
    "retry_async",
    "setup_logging",
    "truncate",
]
