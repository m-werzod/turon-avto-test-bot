"""Publishing a question as a Telegram quiz poll.

The Bot API has no way to attach an image *to* a poll, so an illustrated question
goes out as two messages: the picture (carrying the question as its caption),
immediately followed by the quiz poll itself. Both carry the full question text
so each message stands on its own in the channel feed.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from aiogram import Bot
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramRetryAfter,
)
from aiogram.types import FSInputFile, Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot.database.models.channel import Channel
from bot.database.models.event_log import EventType
from bot.database.models.question import Question
from bot.database.models.quiz_post import PostTrigger, QuizPost
from bot.database.repositories import (
    ChannelRepository,
    CycleRepository,
    DeliveryRepository,
    EventRepository,
    QuestionRepository,
    SettingsRepository,
)
from bot.locales.i18n import t
from bot.services.media_service import MediaService
from bot.utils.logging import get_logger
from bot.utils.text import (
    CAPTION_LIMIT,
    POLL_EXPLANATION_LIMIT,
    POLL_QUESTION_LIMIT,
    escape_html,
    normalize_for_poll,
    truncate,
)

logger = get_logger(__name__)

#: Errors that mean the bot has lost access for good, not transiently.
_ACCESS_ERROR_MARKERS = (
    "chat not found",
    "bot was kicked",
    "bot is not a member",
    "not enough rights",
    "have no rights",
    "chat_write_forbidden",
    "user_banned_in_channel",
)


class NoChannelsError(RuntimeError):
    """No active channel is connected, so there is nowhere to publish."""


@dataclass(slots=True)
class ChannelOutcome:
    """Result of publishing to one channel."""

    channel: str
    ok: bool
    error: str | None = None
    access_lost: bool = False


@dataclass(slots=True)
class SendReport:
    """Summary of one publish operation across every channel."""

    question_id: int
    cycle_number: int
    outcomes: list[ChannelOutcome] = field(default_factory=list)
    cycle_rolled: bool = False
    released: bool = False

    @property
    def succeeded(self) -> int:
        """Channels that received the quiz."""
        return sum(1 for outcome in self.outcomes if outcome.ok)

    @property
    def failed(self) -> int:
        """Channels that did not."""
        return sum(1 for outcome in self.outcomes if not outcome.ok)

    @property
    def fully_failed(self) -> bool:
        """Whether every channel failed."""
        return bool(self.outcomes) and self.succeeded == 0

    def error_summary(self, limit: int = 3) -> str:
        """Newline-joined failures, for an admin message."""
        failures = [f"• {o.channel}: {o.error}" for o in self.outcomes if not o.ok]
        shown = failures[:limit]
        if len(failures) > limit:
            shown.append(f"• … +{len(failures) - limit}")
        return "\n".join(shown)


class QuizService:
    """Claims questions and publishes them to every connected channel."""

    def __init__(
        self,
        bot: Bot,
        media: MediaService,
        *,
        send_as_document: bool = False,
        content_language_default: str = "uz",
    ) -> None:
        """Configure the publisher.

        Args:
            bot: aiogram bot instance.
            media: Used to resolve cached image paths.
            send_as_document: Send images uncompressed as documents. Preserves
                original quality at the cost of not rendering inline.
            content_language_default: Fallback language for question selection.
        """
        self.bot = bot
        self.media = media
        self.send_as_document = send_as_document
        self.content_language_default = content_language_default

    # --- Message construction -------------------------------------------------

    @staticmethod
    def build_caption(question: Question, number: int, language: str) -> str:
        """Build the photo caption carrying the question text."""
        caption = t(
            "quiz.caption",
            language,
            number=number,
            text=escape_html(question.text),
        )
        return truncate(caption, CAPTION_LIMIT)

    @staticmethod
    def build_poll_payload(question: Question) -> dict[str, Any]:
        """Assemble the ``sendPoll`` arguments for a quiz.

        Every field is re-clamped here even though the importer already did so:
        this is the last point before the API call, and an over-long value is
        rejected outright, costing a scheduled post.
        """
        payload: dict[str, Any] = {
            "question": normalize_for_poll(question.text, POLL_QUESTION_LIMIT),
            "options": [normalize_for_poll(option, 100) for option in question.options],
            "type": "quiz",
            "correct_option_id": question.correct_index,
            "is_anonymous": True,
            "allows_multiple_answers": False,
        }
        if question.explanation:
            payload["explanation"] = normalize_for_poll(
                question.explanation, POLL_EXPLANATION_LIMIT
            )
        return payload

    # --- Sending --------------------------------------------------------------

    async def _send_image(
        self, chat_id: int, question: Question, caption: str
    ) -> tuple[Message | None, str | None]:
        """Send the question image, if the question has one.

        Prefers a cached Telegram ``file_id`` — re-uploading the same picture on
        every cycle would waste bandwidth for no gain. A rejected ``file_id``
        falls back to the local file and the caller clears the stale value.

        Returns:
            The sent message (or ``None`` when there is no image), and the
            ``file_id`` to cache.
        """
        if question.image_file_id:
            try:
                message = await self._dispatch_image(chat_id, question.image_file_id, caption)
                return message, question.image_file_id
            except TelegramBadRequest as exc:
                logger.warning(
                    "Cached file_id rejected for question %d (%s); re-uploading",
                    question.id,
                    exc.message,
                )

        if not question.image_path:
            return None, None

        path = self.media.absolute_path(question.image_path)
        if not path.exists():
            logger.warning(
                "Image file missing for question %d: %s — sending poll without it",
                question.id,
                path,
            )
            return None, None

        message = await self._dispatch_image(chat_id, FSInputFile(path), caption)
        return message, self._extract_file_id(message)

    async def _dispatch_image(
        self, chat_id: int, media: str | FSInputFile, caption: str
    ) -> Message:
        """Send an image as a photo or an uncompressed document."""
        if self.send_as_document:
            return await self.bot.send_document(
                chat_id=chat_id, document=media, caption=caption, parse_mode="HTML"
            )
        return await self.bot.send_photo(
            chat_id=chat_id, photo=media, caption=caption, parse_mode="HTML"
        )

    @staticmethod
    def _extract_file_id(message: Message) -> str | None:
        """Pull a reusable ``file_id`` out of a sent message."""
        if message.document is not None:
            return message.document.file_id
        if message.photo:
            # Largest rendition last; that is the one worth caching.
            return message.photo[-1].file_id
        return None

    async def _publish_to_channel(
        self, channel: Channel, question: Question, number: int, language: str
    ) -> tuple[int | None, int | None, str | None]:
        """Publish one question to one channel.

        Returns:
            Poll message id, photo message id, and the cached ``file_id``.

        Raises:
            TelegramForbiddenError: Access to the channel was lost.
            TelegramBadRequest: The request was rejected.
            TelegramNetworkError: The call did not reach Telegram.
        """
        photo_message: Message | None = None
        file_id: str | None = None

        if question.has_image:
            caption = self.build_caption(question, number, language)
            photo_message, file_id = await self._send_image(channel.chat_id, question, caption)

        poll_message = await self.bot.send_poll(
            chat_id=channel.chat_id, **self.build_poll_payload(question)
        )

        return (
            poll_message.message_id,
            photo_message.message_id if photo_message else None,
            file_id,
        )

    @staticmethod
    def _is_access_lost(error: Exception) -> bool:
        """Whether an error means access is gone rather than momentarily broken."""
        if isinstance(error, TelegramForbiddenError):
            return True
        message = str(error).lower()
        return any(marker in message for marker in _ACCESS_ERROR_MARKERS)

    async def send_next(
        self,
        session: AsyncSession,
        *,
        trigger: PostTrigger = PostTrigger.SCHEDULED,
        admin_language: str = "uz",
    ) -> SendReport:
        """Claim the next question and broadcast it to every active channel.

        Args:
            session: Open session; the caller owns the transaction.
            trigger: What prompted this send.
            admin_language: Language for text embedded in the messages.

        Returns:
            A per-channel report.

        Raises:
            NoChannelsError: No active channel is connected.
            CycleExhaustedError: The question bank is empty.
        """
        channels_repo = ChannelRepository(session)
        cycle_repo = CycleRepository(session)
        question_repo = QuestionRepository(session)
        delivery_repo = DeliveryRepository(session)
        settings_repo = SettingsRepository(session)
        events = EventRepository(session)

        channels = await channels_repo.list_active()
        if not channels:
            raise NoChannelsError("No active channels are connected")

        # Widens to None on the fallback path below, meaning "any language".
        language: str | None = await settings_repo.content_language()
        total_active = await question_repo.count_active(language)
        if total_active == 0:
            # Fall back to any language rather than refusing to post, so a bank
            # imported under an unexpected code still reaches the channel.
            language = None
            total_active = await question_repo.count_active()

        post, question, cycle_rolled = await cycle_repo.claim_next_question(
            trigger=trigger, language=language, total_active=total_active
        )
        cycle = await cycle_repo.get(post.cycle_id)
        cycle_number = cycle.number if cycle else 0
        sequence = await cycle_repo.count_posts_in_cycle(post.cycle_id)

        report = SendReport(
            question_id=question.id, cycle_number=cycle_number, cycle_rolled=cycle_rolled
        )

        for channel in channels:
            outcome = await self._deliver(
                session=session,
                channel=channel,
                question=question,
                post=post,
                sequence=sequence,
                language=admin_language,
                delivery_repo=delivery_repo,
                channels_repo=channels_repo,
                question_repo=question_repo,
            )
            report.outcomes.append(outcome)

        if report.fully_failed:
            # Nothing reached an audience. Releasing the claim keeps the question
            # in the pool so a network outage costs a slot, not a question — the
            # alternative silently shrinks the cycle every time Telegram blips.
            await session.delete(post)
            await session.flush()
            report.released = True
            await events.record(
                EventType.QUIZ_FAILED,
                f"Question #{question.id} could not be delivered to any channel; "
                f"claim released back to cycle #{cycle_number}",
                level="ERROR",
                payload={"question_id": question.id, "cycle": cycle_number},
            )
            logger.error(
                "Delivery failed on every channel; released question %d back to cycle %d",
                question.id,
                cycle_number,
            )
        else:
            await events.record(
                EventType.QUIZ_SENT,
                f"Question #{question.id} sent to {report.succeeded} channel(s) "
                f"(cycle #{cycle_number}, {sequence}/{total_active})",
                payload={
                    "question_id": question.id,
                    "cycle": cycle_number,
                    "sequence": sequence,
                    "ok": report.succeeded,
                    "failed": report.failed,
                    "trigger": trigger.value,
                },
            )

        if cycle_rolled:
            await events.record(
                EventType.CYCLE_STARTED,
                f"All questions used; cycle #{cycle_number} started",
                payload={"cycle": cycle_number},
            )

        return report

    async def _deliver(
        self,
        *,
        session: AsyncSession,
        channel: Channel,
        question: Question,
        post: QuizPost,
        sequence: int,
        language: str,
        delivery_repo: DeliveryRepository,
        channels_repo: ChannelRepository,
        question_repo: QuestionRepository,
    ) -> ChannelOutcome:
        """Publish to one channel and record the outcome.

        Never raises: a broken channel is recorded and reported, because one
        unreachable channel must not abort the broadcast to the others.
        """
        delivery = await delivery_repo.create_pending(post.id, channel.id)
        label = channel.display_name

        try:
            poll_id, photo_id, file_id = await self._publish_to_channel(
                channel, question, sequence, language
            )
        except TelegramRetryAfter as exc:
            # Flood control. Honour the stated wait once, then give up for this
            # slot rather than blocking the whole broadcast indefinitely.
            logger.warning("Rate limited on %s; waiting %ds", label, exc.retry_after)
            await asyncio.sleep(min(exc.retry_after, 60))
            try:
                poll_id, photo_id, file_id = await self._publish_to_channel(
                    channel, question, sequence, language
                )
            except Exception as retry_exc:  # noqa: BLE001 - recorded below
                return await self._record_failure(
                    session, delivery, channel, retry_exc, delivery_repo, channels_repo
                )
        except (TelegramForbiddenError, TelegramBadRequest, TelegramNetworkError) as exc:
            return await self._record_failure(
                session, delivery, channel, exc, delivery_repo, channels_repo
            )
        except Exception as exc:
            logger.exception("Unexpected error sending to %s", label)
            return await self._record_failure(
                session, delivery, channel, exc, delivery_repo, channels_repo
            )

        await delivery_repo.mark_sent(delivery, poll_message_id=poll_id, photo_message_id=photo_id)
        if file_id and file_id != question.image_file_id:
            await question_repo.set_file_id(question.id, file_id)

        logger.info(
            "Sent question %d to %s (poll message %s)",
            question.id,
            label,
            poll_id,
            extra={"question_id": question.id, "channel": label},
        )
        return ChannelOutcome(channel=label, ok=True)

    async def _record_failure(
        self,
        session: AsyncSession,
        delivery: object,
        channel: Channel,
        error: Exception,
        delivery_repo: DeliveryRepository,
        channels_repo: ChannelRepository,
    ) -> ChannelOutcome:
        """Store a failure and deactivate the channel when access is gone."""
        reason = str(error).strip() or type(error).__name__
        label = channel.display_name
        access_lost = self._is_access_lost(error)

        await delivery_repo.mark_failed(delivery, reason)  # type: ignore[arg-type]
        await channels_repo.mark_failed(channel, reason)

        if access_lost:
            # Keep retrying a channel we have been removed from and every future
            # send logs the same error forever; deactivate and tell the admin.
            await channels_repo.deactivate(channel)
            await EventRepository(session).record(
                EventType.CHANNEL_CHECK_FAILED,
                f"Lost access to {label}: {reason}. Channel deactivated.",
                level="ERROR",
                payload={"channel": label, "reason": reason},
            )
            logger.error("Lost access to %s (%s); channel deactivated", label, reason)
        else:
            logger.error("Failed to send to %s: %s", label, reason)

        return ChannelOutcome(channel=label, ok=False, error=reason, access_lost=access_lost)
