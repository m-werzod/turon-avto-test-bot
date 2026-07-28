"""Repository behaviour: idempotent import, settings, schedule and stats."""

from __future__ import annotations

from datetime import UTC, datetime, time

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from bot.database.models.delivery import DeliveryStatus
from bot.database.models.question import Question
from bot.database.models.setting import SettingKey
from bot.database.repositories import (
    ChannelRepository,
    CycleRepository,
    DeliveryRepository,
    EventRepository,
    QuestionRepository,
    ScheduleRepository,
    SettingsRepository,
    UserRepository,
)

OPTIONS = ["A", "B", "C", "D"]


def upsert_kwargs(**overrides: object) -> dict[str, object]:
    """Arguments for a representative question upsert."""
    payload: dict[str, object] = {
        "source": "test",
        "external_id": "q1",
        "text": "Original question?",
        "options": OPTIONS,
        "correct_index": 1,
        "explanation": "Because.",
    }
    payload.update(overrides)
    return payload


class TestQuestionUpsert:
    """Re-import must be idempotent — that is what makes "Update tests" safe."""

    async def test_first_import_creates(self, session: AsyncSession) -> None:
        repo = QuestionRepository(session)
        _, action = await repo.upsert(**upsert_kwargs())  # type: ignore[arg-type]
        assert action == "created"
        assert await repo.count_active() == 1

    async def test_reimport_of_identical_data_is_a_no_op(self, session: AsyncSession) -> None:
        repo = QuestionRepository(session)
        await repo.upsert(**upsert_kwargs())  # type: ignore[arg-type]

        for _ in range(3):
            _, action = await repo.upsert(**upsert_kwargs())  # type: ignore[arg-type]
            assert action == "unchanged"

        assert await repo.count_active() == 1, "re-import created duplicates"

    async def test_cosmetic_whitespace_is_not_a_change(self, session: AsyncSession) -> None:
        """Upstream reformatting must not present as a content change."""
        repo = QuestionRepository(session)
        await repo.upsert(**upsert_kwargs())  # type: ignore[arg-type]
        _, action = await repo.upsert(
            **upsert_kwargs(text="Original    question?")  # type: ignore[arg-type]
        )
        assert action == "unchanged"

    async def test_real_edit_updates(self, session: AsyncSession) -> None:
        repo = QuestionRepository(session)
        await repo.upsert(**upsert_kwargs())  # type: ignore[arg-type]
        question, action = await repo.upsert(
            **upsert_kwargs(text="Corrected question?")  # type: ignore[arg-type]
        )
        assert action == "updated"
        assert question.text == "Corrected question?"
        assert await repo.count_active() == 1

    async def test_changed_answer_updates(self, session: AsyncSession) -> None:
        repo = QuestionRepository(session)
        await repo.upsert(**upsert_kwargs())  # type: ignore[arg-type]
        question, action = await repo.upsert(**upsert_kwargs(correct_index=3))  # type: ignore[arg-type]
        assert action == "updated"
        assert question.correct_index == 3

    async def test_new_image_url_invalidates_the_cached_copy(self, session: AsyncSession) -> None:
        """A changed picture must not keep serving the old cached file."""
        repo = QuestionRepository(session)
        question, _ = await repo.upsert(
            **upsert_kwargs(image_url="https://example.com/a.png")  # type: ignore[arg-type]
        )
        question.image_path = "ab/cached.png"
        question.image_file_id = "STALE_FILE_ID"
        await session.flush()

        updated, _ = await repo.upsert(
            **upsert_kwargs(text="Changed?", image_url="https://example.com/b.png")  # type: ignore[arg-type]
        )
        assert updated.image_path is None
        assert updated.image_file_id is None

    async def test_same_external_id_in_a_different_source_is_separate(
        self, session: AsyncSession
    ) -> None:
        repo = QuestionRepository(session)
        await repo.upsert(**upsert_kwargs(source="alpha"))  # type: ignore[arg-type]
        await repo.upsert(**upsert_kwargs(source="beta"))  # type: ignore[arg-type]
        assert await repo.count_active() == 2

    async def test_deactivate_missing(self, session: AsyncSession) -> None:
        repo = QuestionRepository(session)
        for index in range(3):
            await repo.upsert(**upsert_kwargs(external_id=f"q{index}"))  # type: ignore[arg-type]

        removed = await repo.deactivate_missing("test", {"q0", "q1"})
        assert removed == 1
        assert await repo.count_active() == 2

    async def test_content_hash_is_order_sensitive(self) -> None:
        """Reordering options changes meaning, so it must change the hash."""
        first = Question.compute_content_hash("Q?", ["a", "b", "c", "d"], 0)
        second = Question.compute_content_hash("Q?", ["b", "a", "c", "d"], 0)
        assert first != second


class TestSettings:
    """Typed access over the key/value table."""

    async def test_defaults_apply_before_any_write(self, session: AsyncSession) -> None:
        repo = SettingsRepository(session)
        assert await repo.is_scheduler_paused() is False
        assert await repo.content_language() == "uz"

    async def test_round_trip(self, session: AsyncSession) -> None:
        repo = SettingsRepository(session)
        await repo.set_scheduler_paused(True)
        assert await repo.is_scheduler_paused() is True
        await repo.set_scheduler_paused(False)
        assert await repo.is_scheduler_paused() is False

    @pytest.mark.parametrize("raw", ["true", "TRUE", "1", "yes", "on"])
    async def test_truthy_spellings(self, session: AsyncSession, raw: str) -> None:
        repo = SettingsRepository(session)
        await repo.set_raw(SettingKey.SCHEDULER_PAUSED, raw)
        assert await repo.is_scheduler_paused() is True

    async def test_malformed_int_falls_back(self, session: AsyncSession) -> None:
        """A corrupt value must not crash the caller."""
        repo = SettingsRepository(session)
        await repo.set_raw(SettingKey.POSTS_PER_DAY, "not-a-number")
        assert await repo.get_int(SettingKey.POSTS_PER_DAY, default=3) == 3

    async def test_all_as_dict_includes_defaults(self, session: AsyncSession) -> None:
        repo = SettingsRepository(session)
        await repo.set_content_language("ru")
        values = await repo.all_as_dict()
        assert values[SettingKey.CONTENT_LANGUAGE] == "ru"
        assert SettingKey.SCHEDULER_PAUSED in values


class TestSchedule:
    """Posting times."""

    async def test_replace_all_sorts_and_deduplicates(self, session: AsyncSession) -> None:
        repo = ScheduleRepository(session)
        slots = await repo.replace_all([time(20, 0), time(8, 0), time(13, 0), time(8, 0)])
        assert [slot.label for slot in slots] == ["08:00", "13:00", "20:00"]

    async def test_replace_all_removes_old_slots(self, session: AsyncSession) -> None:
        """Going from three posts a day to one must not orphan two slots."""
        repo = ScheduleRepository(session)
        await repo.replace_all([time(8, 0), time(13, 0), time(20, 0)])
        await repo.replace_all([time(9, 30)])
        remaining = await repo.list_enabled()
        assert [slot.label for slot in remaining] == ["09:30"]

    async def test_add_slot_is_idempotent(self, session: AsyncSession) -> None:
        repo = ScheduleRepository(session)
        _, created_first = await repo.add_slot(time(8, 0))
        _, created_again = await repo.add_slot(time(8, 0))
        assert created_first is True
        assert created_again is False
        assert len(await repo.list_enabled()) == 1


class TestChannels:
    """Connected channels."""

    async def test_reconnecting_revives_the_original_row(self, session: AsyncSession) -> None:
        """History must stay attached rather than being orphaned."""
        repo = ChannelRepository(session)
        channel, created = await repo.upsert(chat_id=-100123, username="ch", title="Ch")
        assert created is True
        original_id = channel.id

        await repo.deactivate(channel)
        assert await repo.list_active() == []

        revived, created_again = await repo.upsert(chat_id=-100123, username="ch", title="Renamed")
        assert created_again is False
        assert revived.id == original_id
        assert revived.is_active is True
        assert revived.title == "Renamed"

    async def test_failure_reason_is_recorded(self, session: AsyncSession) -> None:
        repo = ChannelRepository(session)
        channel, _ = await repo.upsert(chat_id=-100999, username=None, title="X")
        await repo.mark_failed(channel, "bot was kicked")
        assert channel.last_error == "bot was kicked"


class TestDeliveries:
    """Delivery counters behind the statistics panel."""

    async def test_today_uses_the_configured_timezone(
        self, session: AsyncSession, question_bank
    ) -> None:
        """ "Today" must mean the admin's day in Tashkent, not the server's UTC day."""
        from zoneinfo import ZoneInfo

        tashkent = ZoneInfo("Asia/Tashkent")
        channels = ChannelRepository(session)
        channel, _ = await channels.upsert(chat_id=-1, username="c", title="C")

        post, _, _ = await CycleRepository(session).claim_next_question(total_active=25)
        deliveries = DeliveryRepository(session)
        delivery = await deliveries.create_pending(post.id, channel.id)
        await deliveries.mark_sent(delivery, poll_message_id=1)

        today = datetime.now(tashkent).date()
        assert await deliveries.count_sent_on(today, tashkent) == 1
        assert await deliveries.count_sent() == 1

    async def test_failed_deliveries_are_counted_separately(
        self, session: AsyncSession, question_bank
    ) -> None:
        channels = ChannelRepository(session)
        channel, _ = await channels.upsert(chat_id=-2, username="c", title="C")
        post, _, _ = await CycleRepository(session).claim_next_question(total_active=25)

        deliveries = DeliveryRepository(session)
        delivery = await deliveries.create_pending(post.id, channel.id)
        await deliveries.mark_failed(delivery, "not enough rights")

        assert await deliveries.count_sent() == 0
        assert await deliveries.count_failed() == 1
        assert delivery.status is DeliveryStatus.FAILED


class TestUsers:
    """User records and language preference."""

    async def test_touch_creates_then_updates(self, session: AsyncSession) -> None:
        repo = UserRepository(session)
        user, created = await repo.touch(telegram_id=7, username="old", first_name="A")
        assert created is True
        assert user.username == "old"

        again, created_again = await repo.touch(telegram_id=7, username="new", first_name="A")
        assert created_again is False
        assert again.id == user.id
        assert again.username == "new"

    async def test_touch_never_resets_a_chosen_language(self, session: AsyncSession) -> None:
        """The language is the user's own choice; an incidental update must not undo it."""
        repo = UserRepository(session)
        await repo.touch(telegram_id=7, default_language="uz")
        await repo.set_language(7, "ru")

        await repo.touch(telegram_id=7, default_language="uz")
        assert await repo.get_language(7) == "ru"


class TestEventLog:
    """Audit trail."""

    async def test_recent_returns_newest_first(self, session: AsyncSession) -> None:
        repo = EventRepository(session)
        for index in range(5):
            await repo.record("quiz_sent", f"message {index}")

        recent = await repo.recent(limit=3)
        assert len(recent) == 3
        assert recent[0].message == "message 4"

    async def test_level_filter(self, session: AsyncSession) -> None:
        repo = EventRepository(session)
        await repo.record("quiz_sent", "fine")
        await repo.record("quiz_failed", "broken", level="ERROR")

        errors = await repo.recent(level="ERROR")
        assert len(errors) == 1
        assert errors[0].message == "broken"

    async def test_purge_keeps_recent_rows(self, session: AsyncSession) -> None:
        repo = EventRepository(session)
        await repo.record("bot_started", "recent")
        removed = await repo.purge_older_than(days=30)
        assert removed == 0
        assert len(await repo.recent()) == 1

    async def test_payload_round_trips(self, session: AsyncSession) -> None:
        repo = EventRepository(session)
        await repo.record("quiz_sent", "with payload", payload={"question_id": 42, "ok": True})
        entry = (await repo.recent(limit=1))[0]
        assert entry.payload == {"question_id": 42, "ok": True}

        # SQLite has no native timestamptz and hands back a naive datetime even
        # for DateTime(timezone=True); PostgreSQL returns an aware one. Code that
        # compares these timestamps (the scheduler's catch-up check, the stats
        # formatter) normalises tzinfo for exactly this reason, so the test does
        # the same rather than assuming one backend.
        created_at = entry.created_at
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=UTC)
        assert created_at <= datetime.now(UTC)


class TestNewUserTimestamps:
    """Regression: reading a server-default column right after INSERT.

    ``created_at`` / ``updated_at`` carry ``server_default=func.now()``, so the
    ORM has no value for them until a round-trip fetches one. Under asyncio that
    refresh is synchronous IO and raises MissingGreenlet, which crashed every
    brand-new user's first /start.
    """

    async def test_timestamps_readable_after_creation(self, session: AsyncSession) -> None:
        repo = UserRepository(session)

        user, _ = await repo.touch(telegram_id=999001, first_name="New")

        assert user.created_at is not None
        assert user.updated_at is not None

    async def test_timestamps_readable_after_a_returning_user_is_touched(
        self, session: AsyncSession
    ) -> None:
        """The real failure: ``onupdate`` expires ``updated_at`` on every UPDATE.

        An INSERT populates both timestamps through RETURNING, so a first-time
        user is fine. The second touch emits an UPDATE, and ``updated_at`` is
        left expired because only the database knows its new value — so the next
        read of it attempts synchronous IO.
        """
        repo = UserRepository(session)
        await repo.touch(telegram_id=999002, first_name="Seen")

        user, _ = await repo.touch(telegram_id=999002, first_name="Seen again")

        # The access itself is the assertion: before the fix this raised
        # sqlalchemy.exc.MissingGreenlet rather than returning a value.
        assert user.created_at is not None
        assert user.updated_at is not None
