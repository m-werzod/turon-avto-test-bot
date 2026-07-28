"""Poll construction and the broadcast/failure semantics of publishing."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from bot.database.models.delivery import DeliveryStatus
from bot.database.models.question import Question
from bot.database.models.quiz_post import PostTrigger
from bot.database.repositories import ChannelRepository, DeliveryRepository
from bot.services.media_service import MediaService
from bot.services.quiz_service import NoChannelsError, QuizService
from bot.utils.text import POLL_EXPLANATION_LIMIT, POLL_OPTION_LIMIT, POLL_QUESTION_LIMIT
from tests.conftest import make_question


@dataclass
class FakeMessage:
    """Stand-in for an aiogram Message."""

    message_id: int = 1
    document: Any = None
    photo: list[Any] = field(default_factory=list)


@dataclass
class FakeBot:
    """Records calls instead of talking to Telegram.

    A fake rather than a mock so assertions are about the payload actually built,
    which is where the interesting bugs (length limits, correct_option_id) live.
    """

    id: int = 999
    polls: list[dict[str, Any]] = field(default_factory=list)
    photos: list[dict[str, Any]] = field(default_factory=list)
    fail_on: set[int] = field(default_factory=set)
    _next_message_id: int = 100

    async def send_poll(self, **kwargs: Any) -> FakeMessage:
        chat_id = kwargs["chat_id"]
        if chat_id in self.fail_on:
            raise RuntimeError("simulated send failure")
        self.polls.append(kwargs)
        self._next_message_id += 1
        return FakeMessage(message_id=self._next_message_id)

    async def send_photo(self, **kwargs: Any) -> FakeMessage:
        chat_id = kwargs["chat_id"]
        if chat_id in self.fail_on:
            raise RuntimeError("simulated send failure")
        self.photos.append(kwargs)
        self._next_message_id += 1
        return FakeMessage(message_id=self._next_message_id)

    async def send_document(self, **kwargs: Any) -> FakeMessage:
        return await self.send_photo(**kwargs)


@pytest.fixture
def quiz_service(tmp_path) -> tuple[QuizService, FakeBot]:  # type: ignore[no-untyped-def]
    """A quiz service wired to a fake bot."""
    bot = FakeBot()
    media = MediaService(tmp_path / "media")
    return QuizService(bot, media), bot  # type: ignore[arg-type]


class TestPollPayload:
    """The payload must satisfy Telegram before it is ever sent."""

    def test_is_a_quiz_with_the_correct_option(self) -> None:
        question = Question(
            source="t",
            external_id="1",
            text="Question?",
            options=["A", "B", "C", "D"],
            correct_index=2,
            content_hash="x",
        )
        payload = QuizService.build_poll_payload(question)

        assert payload["type"] == "quiz"
        assert payload["correct_option_id"] == 2
        assert payload["options"] == ["A", "B", "C", "D"]
        assert payload["is_anonymous"] is True
        assert payload["allows_multiple_answers"] is False

    def test_every_field_respects_its_limit(self) -> None:
        """An over-long field is rejected by Telegram and costs a scheduled post."""
        question = Question(
            source="t",
            external_id="1",
            text="Q" * 500,
            options=["A" * 200, "B" * 200, "C" * 200, "D" * 200],
            correct_index=0,
            explanation="E" * 500,
            content_hash="x",
        )
        payload = QuizService.build_poll_payload(question)

        assert len(payload["question"]) <= POLL_QUESTION_LIMIT  # type: ignore[arg-type]
        assert all(len(option) <= POLL_OPTION_LIMIT for option in payload["options"])  # type: ignore[union-attr]
        assert len(payload["explanation"]) <= POLL_EXPLANATION_LIMIT  # type: ignore[arg-type]

    def test_explanation_omitted_when_absent(self) -> None:
        question = Question(
            source="t",
            external_id="1",
            text="Q?",
            options=["A", "B", "C", "D"],
            correct_index=0,
            explanation=None,
            content_hash="x",
        )
        assert "explanation" not in QuizService.build_poll_payload(question)


class TestSendNext:
    """Broadcasting to channels."""

    async def _add_channel(self, session: AsyncSession, chat_id: int) -> None:
        await ChannelRepository(session).upsert(
            chat_id=chat_id, username=f"c{abs(chat_id)}", title="Channel"
        )

    async def test_requires_a_channel(
        self, session: AsyncSession, question_bank, quiz_service
    ) -> None:
        service, _ = quiz_service
        with pytest.raises(NoChannelsError):
            await service.send_next(session)

    async def test_sends_one_poll_per_channel(
        self, session: AsyncSession, question_bank, quiz_service
    ) -> None:
        service, bot = quiz_service
        for chat_id in (-101, -102, -103):
            await self._add_channel(session, chat_id)

        report = await service.send_next(session, trigger=PostTrigger.MANUAL)

        assert report.succeeded == 3
        assert report.failed == 0
        assert len(bot.polls) == 3
        assert {poll["chat_id"] for poll in bot.polls} == {-101, -102, -103}

    async def test_one_broken_channel_does_not_stop_the_others(
        self, session: AsyncSession, question_bank, quiz_service
    ) -> None:
        service, bot = quiz_service
        for chat_id in (-201, -202, -203):
            await self._add_channel(session, chat_id)
        bot.fail_on = {-202}

        report = await service.send_next(session)

        assert report.succeeded == 2
        assert report.failed == 1
        assert report.released is False, "a partial success must keep the claim"

    async def test_total_failure_releases_the_question(
        self, session: AsyncSession, question_bank, quiz_service
    ) -> None:
        """An outage should cost a slot, not a question.

        Keeping the claim after reaching nobody would silently shrink the cycle
        every time Telegram blips.
        """
        service, bot = quiz_service
        await self._add_channel(session, -301)
        bot.fail_on = {-301}

        report = await service.send_next(session)

        assert report.fully_failed is True
        assert report.released is True

        from bot.database.repositories import CycleRepository

        cycles = CycleRepository(session)
        cycle = await cycles.get_open_cycle()
        assert cycle is not None
        assert await cycles.count_posts_in_cycle(cycle.id) == 0, "the claim was not released"
        assert await cycles.count_remaining(cycle.id) == len(question_bank)

    async def test_delivery_rows_record_the_outcome(
        self, session: AsyncSession, question_bank, quiz_service
    ) -> None:
        service, bot = quiz_service
        await self._add_channel(session, -401)
        await self._add_channel(session, -402)
        bot.fail_on = {-402}

        await service.send_next(session)

        deliveries = await DeliveryRepository(session).recent(limit=10)
        statuses = {delivery.status for delivery in deliveries}
        assert statuses == {DeliveryStatus.SENT, DeliveryStatus.FAILED}

        failed = next(d for d in deliveries if d.status is DeliveryStatus.FAILED)
        assert failed.error_message

    async def test_repeated_sends_never_repeat_a_question(
        self, session: AsyncSession, question_bank, quiz_service
    ) -> None:
        """The end-to-end version of the no-repeat guarantee."""
        service, bot = quiz_service
        await self._add_channel(session, -501)

        for _ in range(len(question_bank)):
            await service.send_next(session)

        asked = [poll["question"] for poll in bot.polls]
        assert len(asked) == len(question_bank)
        assert len(set(asked)) == len(question_bank), "a question was published twice"


class TestSendBatch:
    """Publishing several questions at one scheduled time."""

    async def _add_channel(self, session: AsyncSession, chat_id: int) -> None:
        await ChannelRepository(session).upsert(
            chat_id=chat_id, username=f"c{abs(chat_id)}", title="Channel"
        )

    async def test_sends_the_requested_number(
        self, session: AsyncSession, question_bank, quiz_service
    ) -> None:
        service, bot = quiz_service
        await self._add_channel(session, -301)

        reports = await service.send_batch(session, 5, pause_between=0)

        assert len(reports) == 5
        assert len(bot.polls) == 5

    async def test_questions_within_a_batch_are_all_different(
        self, session: AsyncSession, question_bank, quiz_service
    ) -> None:
        """The no-repeat guarantee has to hold inside a batch too."""
        service, _ = quiz_service
        await self._add_channel(session, -302)

        reports = await service.send_batch(session, 8, pause_between=0)

        question_ids = [report.question_id for report in reports]
        assert len(set(question_ids)) == len(question_ids)

    async def test_a_batch_larger_than_the_cycle_rolls_into_the_next_one(
        self, session: AsyncSession, question_bank, quiz_service
    ) -> None:
        """Exhausting the cycle mid-batch starts the next one and carries on.

        Running out is not an error: the spec calls for a fresh cycle once every
        question has been used, so a batch spanning that boundary keeps sending
        rather than stopping short.
        """
        service, bot = quiz_service
        await self._add_channel(session, -303)

        reports = await service.send_batch(session, 30, pause_between=0)

        assert len(reports) == 30
        assert len(bot.polls) == 30
        assert sum(report.cycle_rolled for report in reports) == 1, "cycle should roll exactly once"

    async def test_zero_sends_nothing(
        self, session: AsyncSession, question_bank, quiz_service
    ) -> None:
        service, bot = quiz_service
        await self._add_channel(session, -304)

        reports = await service.send_batch(session, 0, pause_between=0)

        assert reports == []
        assert bot.polls == []


class TestSingleMessageDelivery:
    """An illustrated question must arrive as one post, not two.

    Before the Bot API allowed media on a poll this took a photo message plus a
    poll message, so a reader scrolling the channel met options with no picture
    or a picture whose options had not arrived. The picture now rides inside the
    poll.
    """

    async def _add_channel(self, session: AsyncSession, chat_id: int) -> None:
        await ChannelRepository(session).upsert(
            chat_id=chat_id, username=f"c{abs(chat_id)}", title="Channel"
        )

    async def _question_with_image(self, session: AsyncSession, media: MediaService) -> Question:
        """Store one question whose image exists on disk."""
        question = make_question(1)
        relative = "ab/picture.jpg"
        path = media.absolute_path(relative)
        path.parent.mkdir(parents=True, exist_ok=True)
        # Smallest valid JPEG the service will accept as a real file.
        path.write_bytes(bytes.fromhex("ffd8ffdb004300ff") + b"\x00" * 32 + bytes.fromhex("ffd9"))
        question.image_url = "https://example.uz/picture.jpg"
        question.image_path = relative
        session.add(question)
        await session.flush()
        return question

    async def test_illustrated_question_sends_exactly_one_message(
        self, session: AsyncSession, quiz_service
    ) -> None:
        service, bot = quiz_service
        await self._add_channel(session, -401)
        await self._question_with_image(session, service.media)

        await service.send_next(session, trigger=PostTrigger.MANUAL)

        assert len(bot.polls) == 1, "expected a single poll message"
        assert bot.photos == [], "the photo must not be sent as its own message"

    async def test_the_image_travels_on_the_poll(
        self, session: AsyncSession, quiz_service
    ) -> None:
        service, bot = quiz_service
        await self._add_channel(session, -402)
        await self._question_with_image(session, service.media)

        await service.send_next(session, trigger=PostTrigger.MANUAL)

        assert bot.polls[0].get("media") is not None

    async def test_question_without_an_image_still_sends_a_plain_poll(
        self, session: AsyncSession, question_bank, quiz_service
    ) -> None:
        service, bot = quiz_service
        await self._add_channel(session, -403)

        await service.send_next(session, trigger=PostTrigger.MANUAL)

        assert len(bot.polls) == 1
        assert bot.polls[0].get("media") is None
        assert bot.photos == []

    async def test_a_missing_image_file_does_not_cost_the_post(
        self, session: AsyncSession, quiz_service
    ) -> None:
        """A cached path pointing at a deleted file must degrade, not fail."""
        service, bot = quiz_service
        await self._add_channel(session, -404)

        question = make_question(2)
        question.image_path = "zz/vanished.jpg"
        session.add(question)
        await session.flush()

        report = await service.send_next(session, trigger=PostTrigger.MANUAL)

        assert report.succeeded == 1
        assert bot.polls[0].get("media") is None
