"""Pushing operational notices to admins over Telegram."""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from datetime import datetime
from zoneinfo import ZoneInfo

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError

from bot.locales.i18n import t
from bot.utils.logging import get_logger
from bot.utils.text import MESSAGE_LIMIT, escape_html, truncate

logger = get_logger(__name__)

#: Minimum gap between identical alerts, so a failing job cannot spam an admin.
_DEDUPE_WINDOW_SECONDS = 300


class NotifyService:
    """Sends alerts to every configured admin.

    Delivery failures are swallowed on purpose: an alert that cannot be sent must
    never take down the thing it was reporting on.
    """

    def __init__(
        self,
        bot: Bot,
        admin_ids: Iterable[int],
        *,
        enabled: bool = True,
        timezone: ZoneInfo | None = None,
    ) -> None:
        self.bot = bot
        self.admin_ids = tuple(admin_ids)
        self.enabled = enabled
        self.timezone = timezone or ZoneInfo("Asia/Tashkent")
        self._recent: dict[str, float] = {}

    def _should_send(self, fingerprint: str) -> bool:
        """Rate-limit repeats of the same alert."""
        now = asyncio.get_running_loop().time()
        last = self._recent.get(fingerprint)
        if last is not None and now - last < _DEDUPE_WINDOW_SECONDS:
            return False
        self._recent[fingerprint] = now

        # Keep the dedupe table small on a long-running process.
        if len(self._recent) > 200:
            cutoff = now - _DEDUPE_WINDOW_SECONDS
            self._recent = {key: ts for key, ts in self._recent.items() if ts > cutoff}
        return True

    async def to_user(self, user_id: int, text: str, *, fingerprint: str | None = None) -> int:
        """Send a message to one specific user.

        Operational problems now belong to whoever owns the schedule that hit
        them, not to the installation's admins: a channel the bot was removed
        from is that person's to fix, and telling everyone else about it is both
        noise and a small leak of who runs what.

        Args:
            user_id: Telegram id of the recipient.
            text: HTML-formatted message.
            fingerprint: Dedupe key. Repeats within five minutes are dropped.
                Include the user id in it, or one person's repeated failure will
                silence the same warning for everybody else.

        Returns:
            1 when delivered, 0 otherwise.
        """
        if not self.enabled or not user_id:
            return 0
        if fingerprint and not self._should_send(fingerprint):
            logger.debug("Suppressed duplicate notification: %s", fingerprint)
            return 0

        try:
            await self.bot.send_message(
                chat_id=user_id,
                text=truncate(text, MESSAGE_LIMIT),
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
        except TelegramAPIError as exc:
            # Usually the user blocked the bot or never opened a chat with it —
            # nothing the caller can act on.
            logger.warning("Could not notify user %d: %s", user_id, exc)
            return 0
        except Exception:
            logger.exception("Unexpected error notifying user %d", user_id)
            return 0

        return 1

    async def broadcast(self, text: str, *, fingerprint: str | None = None) -> int:
        """Send a message to every admin.

        Args:
            text: HTML-formatted message.
            fingerprint: Dedupe key. Repeats within five minutes are dropped.

        Returns:
            How many admins received it.
        """
        if not self.enabled or not self.admin_ids:
            return 0
        if fingerprint and not self._should_send(fingerprint):
            logger.debug("Suppressed duplicate notification: %s", fingerprint)
            return 0

        body = truncate(text, MESSAGE_LIMIT)
        delivered = 0

        for admin_id in self.admin_ids:
            try:
                await self.bot.send_message(
                    chat_id=admin_id,
                    text=body,
                    parse_mode="HTML",
                    disable_web_page_preview=True,
                )
                delivered += 1
            except TelegramAPIError as exc:
                # Most often the admin has simply never opened a chat with the
                # bot, which is not something the caller can act on.
                logger.warning("Could not notify admin %d: %s", admin_id, exc)
            except Exception:
                logger.exception("Unexpected error notifying admin %d", admin_id)

        return delivered

    async def notify_error(self, where: str, error: BaseException, *, language: str = "uz") -> int:
        """Report an unhandled error.

        Args:
            where: Component that failed, e.g. ``"scheduler:08:00"``.
            error: The exception.
            language: Language for the notice.

        Returns:
            How many admins were reached.
        """
        message = t(
            "errors.admin_notice",
            language,
            where=escape_html(where),
            error=escape_html(f"{type(error).__name__}: {error}"[:300]),
            time=datetime.now(self.timezone).strftime("%d.%m.%Y %H:%M:%S"),
        )
        # Fingerprint on type and location, not the message: the same fault
        # firing every minute usually carries slightly different detail.
        return await self.broadcast(message, fingerprint=f"{where}:{type(error).__name__}")

    async def notify_channel_lost(
        self,
        channel: str,
        reason: str,
        *,
        language: str = "uz",
        user_id: int | None = None,
    ) -> int:
        """Tell the channel's owner the bot can no longer post to it.

        Falls back to the installation admins when no owner is given, which
        covers the paths that are genuinely installation-wide.
        """
        message = t(
            "errors.channel_lost_access",
            language,
            channel=escape_html(channel),
            reason=escape_html(reason[:200]),
        )
        if user_id is not None:
            return await self.to_user(
                user_id, message, fingerprint=f"channel_lost:{user_id}:{channel}"
            )
        return await self.broadcast(message, fingerprint=f"channel_lost:{channel}")

    async def notify_source_unavailable(self, attempts: int, *, language: str = "uz") -> int:
        """Tell admins a content source could not be reached."""
        message = t("errors.source_unavailable", language, attempts=attempts)
        return await self.broadcast(message, fingerprint="source_unavailable")
