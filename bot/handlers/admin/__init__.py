"""Panel routers.

The bot is multi-tenant: anyone can connect their own channel, set their own
schedule and post from the shared question bank. So the panel itself is open, and
only the few features that act on the *installation* rather than on one person's
setup stay behind the operator gate.

:func:`build_panel_router` returns both halves, with the gate already attached to
the restricted one, so the application wiring cannot forget it — a handler added
there is protected by construction rather than by convention.
"""

from aiogram import Router

from bot.handlers.admin import backup, channels, imports, logs, menu, scheduler, settings, stats
from bot.middlewares.admin import OperatorOnlyMiddleware


def build_panel_router(admin_ids: frozenset[int]) -> Router:
    """Assemble the panel, open where it can be and gated where it must be.

    Open to every user, because each only ever sees rows they own: their
    channels, their schedule and batch size, their statistics and cycle, their
    language and pause state.

    Restricted to the operator, because these act on the whole installation:

    * refreshing the question bank — it is shared, and letting every user trigger
      a scrape would hammer the source sites for data everybody already has
    * server logs and database backups — both span every user's activity

    Args:
        admin_ids: Telegram ids of the installation's operators.

    Returns:
        A router covering every panel feature.
    """
    router = Router(name="panel")

    router.include_routers(
        menu.router,
        channels.router,
        scheduler.router,
        stats.router,
        settings.router,
    )

    operator = Router(name="operator")
    guard = OperatorOnlyMiddleware(admin_ids)
    operator.message.middleware(guard)
    operator.callback_query.middleware(guard)
    operator.include_routers(
        imports.router,
        backup.router,
        logs.router,
    )
    router.include_router(operator)

    return router


__all__ = ["build_panel_router"]
