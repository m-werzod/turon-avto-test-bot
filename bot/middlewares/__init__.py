"""Middlewares.

Registration order matters. ``DatabaseMiddleware`` runs outermost so a session
exists before anything else, then ``UserContextMiddleware`` needs that session to
resolve the language, and ``AdminOnlyMiddleware`` is attached only to the admin
router so the gate cannot be forgotten on a newly added handler.
"""

from bot.middlewares.admin import AdminOnlyMiddleware
from bot.middlewares.database import DatabaseMiddleware
from bot.middlewares.logging_mw import LoggingMiddleware
from bot.middlewares.throttling import ThrottlingMiddleware
from bot.middlewares.user_context import UserContextMiddleware

__all__ = [
    "AdminOnlyMiddleware",
    "DatabaseMiddleware",
    "LoggingMiddleware",
    "ThrottlingMiddleware",
    "UserContextMiddleware",
]
