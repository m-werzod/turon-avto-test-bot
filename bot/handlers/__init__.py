"""Update handlers.

Router order is significant. The admin router is included before ``start`` so an
admin pressing a panel button is not intercepted by a public handler, and
``errors`` is registered last so it only sees what nothing else handled.
"""

from aiogram import Router

from bot.handlers import errors, start
from bot.handlers.admin import build_admin_router


def build_root_router(admin_ids: frozenset[int]) -> Router:
    """Build the dispatcher's root router.

    Args:
        admin_ids: Telegram ids allowed into the admin panel.

    Returns:
        A router covering the whole bot.
    """
    root = Router(name="root")
    root.include_router(build_admin_router(admin_ids))
    root.include_router(start.router)
    root.include_router(errors.router)
    return root


__all__ = ["build_root_router"]
