"""Admin panel routers.

:func:`build_admin_router` returns a single router with the admin gate already
attached, so the application wiring cannot forget it — every handler included
here is protected by construction rather than by convention.
"""

from aiogram import Router

from bot.handlers.admin import backup, channels, imports, logs, menu, scheduler, settings, stats
from bot.middlewares.admin import AdminOnlyMiddleware


def build_admin_router(admin_ids: frozenset[int]) -> Router:
    """Assemble the admin router with access control applied.

    Args:
        admin_ids: Telegram ids allowed to use the panel.

    Returns:
        A router covering every admin feature, gated for both messages and
        callback queries.
    """
    router = Router(name="admin")

    guard = AdminOnlyMiddleware(admin_ids)
    router.message.middleware(guard)
    router.callback_query.middleware(guard)

    router.include_routers(
        menu.router,
        channels.router,
        scheduler.router,
        stats.router,
        imports.router,
        backup.router,
        logs.router,
        settings.router,
    )
    return router


__all__ = ["build_admin_router"]
