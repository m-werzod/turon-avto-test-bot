"""Connecting a channel and verifying the bot can actually publish there.

Checking up front is what turns "the 08:00 post silently never appeared" into an
error message the admin sees at connection time, while they still have the
channel settings open.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from aiogram import Bot
from aiogram.enums import ChatMemberStatus, ChatType
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.types import Chat, ChatMemberAdministrator, ChatMemberOwner

from bot.locales.i18n import t
from bot.utils.logging import get_logger
from bot.utils.text import normalize_channel_identifier

logger = get_logger(__name__)


class ChannelCheckError(Exception):
    """A channel failed verification.

    Attributes:
        reason_key: Locale key describing the failure.
        params: Interpolation values for that key.
    """

    def __init__(self, reason_key: str, **params: object) -> None:
        self.reason_key = reason_key
        self.params = params
        super().__init__(reason_key)

    def localized(self, language: str) -> str:
        """Render the failure in the admin's language."""
        return t(self.reason_key, language, **self.params)


@dataclass(slots=True)
class ChannelInfo:
    """A verified channel, ready to be stored."""

    chat_id: int
    title: str
    username: str | None
    missing_permissions: list[str] = field(default_factory=list)

    @property
    def display_name(self) -> str:
        """Username when public, otherwise the title."""
        return f"@{self.username}" if self.username else self.title


class ChannelService:
    """Resolves and verifies channels."""

    def __init__(self, bot: Bot) -> None:
        self.bot = bot

    async def resolve(self, raw_identifier: str) -> Chat:
        """Look up a chat from whatever the admin typed.

        Args:
            raw_identifier: ``@name``, a ``t.me`` link, or a numeric id.

        Returns:
            The resolved chat.

        Raises:
            ChannelCheckError: The input was malformed or the chat is unreachable.
        """
        try:
            identifier = normalize_channel_identifier(raw_identifier)
        except ValueError as exc:
            raise ChannelCheckError("channel.invalid", reason=str(exc)) from exc

        chat_id: str | int = int(identifier) if identifier.lstrip("-").isdigit() else identifier

        try:
            return await self.bot.get_chat(chat_id)
        except TelegramForbiddenError as exc:
            raise ChannelCheckError("channel.bot_not_member") from exc
        except TelegramBadRequest as exc:
            message = str(exc).lower()
            if "not found" in message or "invalid" in message:
                raise ChannelCheckError("channel.not_found") from exc
            raise ChannelCheckError("channel.bot_not_member") from exc

    async def verify(self, raw_identifier: str) -> ChannelInfo:
        """Resolve a channel and confirm the bot can publish quizzes to it.

        Checks, in the order an admin would need to fix them: is it a channel, is
        the bot a member, is the bot an administrator, and does it hold the right
        to post.

        In a channel a single administrator right — *Post messages* — governs
        text, media and polls alike; there are no separate per-type toggles the
        way there are for group members. So one missing right is reported as all
        three affected capabilities, which is what the admin will look for in the
        channel's permission screen.

        Args:
            raw_identifier: Whatever the admin sent.

        Returns:
            The verified channel.

        Raises:
            ChannelCheckError: Any check failed, carrying a localizable reason.
        """
        chat = await self.resolve(raw_identifier)

        if chat.type != ChatType.CHANNEL:
            raise ChannelCheckError("channel.not_a_channel")

        try:
            member = await self.bot.get_chat_member(chat.id, self.bot.id)
        except TelegramForbiddenError as exc:
            raise ChannelCheckError("channel.bot_not_member") from exc
        except TelegramBadRequest as exc:
            raise ChannelCheckError("channel.bot_not_member") from exc

        if member.status == ChatMemberStatus.LEFT or member.status == ChatMemberStatus.KICKED:
            raise ChannelCheckError("channel.bot_not_member")

        if not isinstance(member, ChatMemberOwner | ChatMemberAdministrator):
            raise ChannelCheckError("channel.bot_not_admin")

        # An owner implicitly holds every right; only an administrator can be
        # missing one.
        if isinstance(member, ChatMemberAdministrator) and not member.can_post_messages:
            raise ChannelCheckError(
                "channel.missing_permissions",
                permissions="\n".join(
                    (
                        t("channel.perm_post_messages", "uz"),
                        t("channel.perm_send_media", "uz"),
                        t("channel.perm_send_polls", "uz"),
                    )
                ),
            )

        info = ChannelInfo(
            chat_id=chat.id,
            title=chat.title or str(chat.id),
            username=chat.username,
        )
        logger.info(
            "Channel verified: %s (%d)",
            info.display_name,
            info.chat_id,
            extra={"channel": info.display_name, "chat_id": info.chat_id},
        )
        return info

    async def verify_localized(self, raw_identifier: str, language: str) -> ChannelInfo:
        """Verify a channel, rendering permission errors in ``language``.

        The permission list inside :meth:`verify` is built in the fallback
        language; this re-renders it so the admin reads it in their own.
        """
        try:
            return await self.verify(raw_identifier)
        except ChannelCheckError as exc:
            if exc.reason_key == "channel.missing_permissions":
                raise ChannelCheckError(
                    exc.reason_key,
                    permissions="\n".join(
                        (
                            t("channel.perm_post_messages", language),
                            t("channel.perm_send_media", language),
                            t("channel.perm_send_polls", language),
                        )
                    ),
                ) from exc
            raise
