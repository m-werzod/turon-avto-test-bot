"""Tenant isolation.

Anyone can run the bot over their own channel, so the question every one of these
asks is the same: can one user see, change or consume another's data? A leak here
is not a cosmetic bug — it shows strangers each other's channels and lets one
person's schedule edit silently delete everybody else's.
"""

from __future__ import annotations

from datetime import time

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from bot.database.models.setting import SettingKey
from bot.database.repositories import (
    ChannelAlreadyConnectedError,
    ChannelRepository,
    CycleRepository,
    ScheduleRepository,
    SettingsRepository,
)
from bot.database.repositories.base import OwnedRepository

ALICE = 111_111
BOB = 222_222


class TestConstruction:
    """The unsafe call must be the one that fails."""

    @pytest.mark.parametrize("repo", [ChannelRepository, CycleRepository, ScheduleRepository])
    def test_an_owned_repository_refuses_to_be_built_without_an_owner(
        self, session: AsyncSession, repo: type[OwnedRepository]
    ) -> None:
        with pytest.raises(TypeError):
            repo(session)  # type: ignore[call-arg]

    @pytest.mark.parametrize("bad", [0, None])
    def test_a_falsy_owner_is_rejected(self, session: AsyncSession, bad: object) -> None:
        """Zero would otherwise become a real owner shared by every careless caller."""
        with pytest.raises(ValueError, match="requires a real owner_id"):
            ChannelRepository(session, bad)  # type: ignore[arg-type]


class TestChannelIsolation:
    """Channels belong to whoever connected them."""

    async def test_one_user_does_not_see_anothers_channels(self, session: AsyncSession) -> None:
        await ChannelRepository(session, ALICE).upsert(
            chat_id=-1001, username="alice_channel", title="Alice"
        )

        assert await ChannelRepository(session, BOB).list_active() == []
        assert len(await ChannelRepository(session, ALICE).list_active()) == 1

    async def test_lookup_by_chat_id_is_scoped(self, session: AsyncSession) -> None:
        """Otherwise Bob could inspect a channel by guessing its id."""
        await ChannelRepository(session, ALICE).upsert(chat_id=-1002, username=None, title="Alice")

        assert await ChannelRepository(session, BOB).get_by_chat_id(-1002) is None
        assert await ChannelRepository(session, ALICE).get_by_chat_id(-1002) is not None

    async def test_fetch_by_primary_key_is_scoped(self, session: AsyncSession) -> None:
        """Primary keys travel in callback data, so this is reachable input."""
        channel, _ = await ChannelRepository(session, ALICE).upsert(
            chat_id=-1003, username=None, title="Alice"
        )

        assert await ChannelRepository(session, BOB).get(channel.id) is None
        assert await ChannelRepository(session, ALICE).get(channel.id) is not None


class TestScheduleIsolation:
    """The bug that would have been worst: one edit wiping every schedule."""

    async def test_replacing_a_schedule_leaves_other_users_alone(
        self, session: AsyncSession
    ) -> None:
        await ScheduleRepository(session, ALICE).replace_all([time(8, 0), time(20, 0)])
        await ScheduleRepository(session, BOB).replace_all([time(9, 30)])

        alice = await ScheduleRepository(session, ALICE).list_enabled()
        bob = await ScheduleRepository(session, BOB).list_enabled()

        assert [slot.label for slot in alice] == ["08:00", "20:00"]
        assert [slot.label for slot in bob] == ["09:30"]

    async def test_removing_a_slot_only_removes_your_own(self, session: AsyncSession) -> None:
        await ScheduleRepository(session, ALICE).replace_all([time(8, 0)])
        await ScheduleRepository(session, BOB).replace_all([time(8, 0)])

        removed = await ScheduleRepository(session, BOB).remove_slot(time(8, 0))

        assert removed is True
        assert len(await ScheduleRepository(session, ALICE).list_enabled()) == 1

    async def test_two_users_may_share_a_posting_time(self, session: AsyncSession) -> None:
        """The old schema made run_at globally unique, which forbade this."""
        await ScheduleRepository(session, ALICE).replace_all([time(8, 0)])
        await ScheduleRepository(session, BOB).replace_all([time(8, 0)])

        owners = await ScheduleRepository.owners_due_at(session, time(8, 0))
        assert sorted(owners) == [ALICE, BOB]

    async def test_the_scheduler_registers_one_job_per_distinct_time(
        self, session: AsyncSession
    ) -> None:
        """Shared times must collapse, or the job store grows with every user."""
        await ScheduleRepository(session, ALICE).replace_all([time(8, 0), time(20, 0)])
        await ScheduleRepository(session, BOB).replace_all([time(8, 0)])

        times = await ScheduleRepository.distinct_enabled_times(session)
        assert times == [time(8, 0), time(20, 0)]


class TestSettingsIsolation:
    """One person pausing must not pause the bot for everybody."""

    async def test_pausing_is_per_user(self, session: AsyncSession) -> None:
        await SettingsRepository(session, ALICE).set_scheduler_paused(True)

        assert await SettingsRepository(session, ALICE).is_scheduler_paused() is True
        assert await SettingsRepository(session, BOB).is_scheduler_paused() is False

    async def test_batch_size_is_per_user(self, session: AsyncSession) -> None:
        await SettingsRepository(session, ALICE).set_questions_per_send(10)

        assert await SettingsRepository(session, ALICE).questions_per_send() == 10
        assert await SettingsRepository(session, BOB).questions_per_send() == 1

    async def test_a_global_value_is_visible_to_everyone(self, session: AsyncSession) -> None:
        """The shared question bank's bookkeeping is one fact for the whole install."""
        await SettingsRepository(session).set_raw(SettingKey.LAST_IMPORT_AT, "2026-07-28")

        assert await SettingsRepository(session, ALICE).get_raw(SettingKey.LAST_IMPORT_AT) == (
            "2026-07-28"
        )
        assert await SettingsRepository(session, BOB).get_raw(SettingKey.LAST_IMPORT_AT) == (
            "2026-07-28"
        )

    async def test_a_personal_value_shadows_the_global_one(self, session: AsyncSession) -> None:
        await SettingsRepository(session).set_raw(SettingKey.CONTENT_LANGUAGE, "uz")
        await SettingsRepository(session, BOB).set_raw(SettingKey.CONTENT_LANGUAGE, "ru")

        assert await SettingsRepository(session, ALICE).content_language() == "uz"
        assert await SettingsRepository(session, BOB).content_language() == "ru"


class TestCycleIsolation:
    """Each user works through the shared bank at their own pace."""

    async def test_cycles_are_numbered_per_user(self, session: AsyncSession) -> None:
        alice = await CycleRepository(session, ALICE).get_or_create_open_cycle(10)
        bob = await CycleRepository(session, BOB).get_or_create_open_cycle(10)

        assert alice.number == 1
        assert bob.number == 1, "Bob starts at his own cycle 1, not Alice's 2"
        assert alice.id != bob.id

    async def test_one_users_progress_does_not_consume_anothers(
        self, session: AsyncSession, question_bank
    ) -> None:
        """The whole bank must stay available to a user who has posted nothing."""
        alice_repo = CycleRepository(session, ALICE)
        alice_cycle = await alice_repo.get_or_create_open_cycle(len(question_bank))
        for _ in range(5):
            await alice_repo.claim_next_question(total_active=len(question_bank))

        bob_repo = CycleRepository(session, BOB)
        bob_cycle = await bob_repo.get_or_create_open_cycle(len(question_bank))

        assert await alice_repo.count_remaining(alice_cycle.id) == len(question_bank) - 5
        assert await bob_repo.count_remaining(bob_cycle.id) == len(question_bank)

    async def test_the_same_question_may_go_to_both_users(
        self, session: AsyncSession, question_bank
    ) -> None:
        """The bank is shared; Alice using a question must not deny it to Bob."""
        alice_repo = CycleRepository(session, ALICE)
        await alice_repo.get_or_create_open_cycle(len(question_bank))
        _, alice_question, _ = await alice_repo.claim_next_question(total_active=len(question_bank))

        bob_repo = CycleRepository(session, BOB)
        await bob_repo.get_or_create_open_cycle(len(question_bank))
        seen = set()
        for _ in range(len(question_bank)):
            _, question, _ = await bob_repo.claim_next_question(total_active=len(question_bank))
            seen.add(question.id)

        assert alice_question.id in seen


class TestChannelOwnershipConflict:
    """A chat may only be driven by one owner.

    Two owners posting to the same channel would double-post every question, so
    ``chat_id`` stays globally unique. The interesting part is what the second
    person is told.
    """

    async def test_claiming_a_channel_someone_else_owns_is_refused(
        self, session: AsyncSession
    ) -> None:
        await ChannelRepository(session, ALICE).upsert(
            chat_id=-1009, username="shared", title="Shared"
        )

        with pytest.raises(ChannelAlreadyConnectedError):
            await ChannelRepository(session, BOB).upsert(
                chat_id=-1009, username="shared", title="Shared"
            )

    async def test_the_original_owner_keeps_it(self, session: AsyncSession) -> None:
        await ChannelRepository(session, ALICE).upsert(chat_id=-1010, username="mine", title="Mine")
        with pytest.raises(ChannelAlreadyConnectedError):
            await ChannelRepository(session, BOB).upsert(
                chat_id=-1010, username="mine", title="Mine"
            )

        assert len(await ChannelRepository(session, ALICE).list_active()) == 1
        assert await ChannelRepository(session, BOB).list_active() == []
