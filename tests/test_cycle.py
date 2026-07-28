"""The no-repeat guarantee.

This is the behaviour the whole project is built around: a question must never
appear twice until every question has been posted, after which a new cycle starts
by itself. These tests exhaust a full bank and assert exactly that.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from bot.database.models.cycle import Cycle
from bot.database.models.question import Question
from bot.database.models.quiz_post import PostTrigger, QuizPost
from bot.database.repositories.cycle_repo import CycleExhaustedError, CycleRepository
from tests.conftest import make_question

#: Fixed owner for tests; every per-user row belongs to one tenant.
OWNER = 424242


async def test_claim_opens_first_cycle(session: AsyncSession, question_bank) -> None:
    """The first claim opens cycle #1 and records the question."""
    repo = CycleRepository(session, OWNER)

    post, question, rolled = await repo.claim_next_question(total_active=len(question_bank))

    assert rolled is False
    assert post.question_id == question.id
    assert post.trigger is PostTrigger.SCHEDULED

    cycle = await repo.get_open_cycle()
    assert cycle is not None
    assert cycle.number == 1
    assert cycle.is_open


async def test_no_question_repeats_within_a_cycle(session: AsyncSession, question_bank) -> None:
    """Claiming the whole bank yields every question exactly once."""
    repo = CycleRepository(session, OWNER)
    total = len(question_bank)

    claimed: list[int] = []
    for _ in range(total):
        _, question, rolled = await repo.claim_next_question(total_active=total)
        claimed.append(question.id)
        assert rolled is False, "cycle rolled before the bank was exhausted"

    assert len(claimed) == total
    assert len(set(claimed)) == total, "a question was claimed twice in one cycle"
    assert set(claimed) == {question.id for question in question_bank}


async def test_cycle_rolls_over_when_exhausted(session: AsyncSession, question_bank) -> None:
    """The claim after the last one closes cycle 1 and opens cycle 2."""
    repo = CycleRepository(session, OWNER)
    total = len(question_bank)

    for _ in range(total):
        await repo.claim_next_question(total_active=total)

    first = await repo.get_open_cycle()
    assert first is not None
    assert await repo.count_remaining(first.id) == 0

    _, _, rolled = await repo.claim_next_question(total_active=total)

    assert rolled is True, "a new cycle should have started"

    cycles = list(await session.scalars(select(Cycle).order_by(Cycle.number)))
    assert len(cycles) == 2
    assert cycles[0].completed_at is not None, "the exhausted cycle should be closed"
    assert cycles[1].is_open
    assert cycles[1].number == 2


async def test_second_cycle_reuses_every_question(session: AsyncSession, question_bank) -> None:
    """A fresh cycle makes the whole bank available again."""
    repo = CycleRepository(session, OWNER)
    total = len(question_bank)

    for _ in range(total):
        await repo.claim_next_question(total_active=total)

    second_round: list[int] = []
    for _ in range(total):
        _, question, _ = await repo.claim_next_question(total_active=total)
        second_round.append(question.id)

    assert len(set(second_round)) == total
    assert set(second_round) == {question.id for question in question_bank}


async def test_claim_order_is_randomised(session: AsyncSession) -> None:
    """Questions come out in a shuffled order, not by primary key.

    Uses a large bank so that an accidental sequential scan is overwhelmingly
    unlikely to look random by chance.
    """
    questions = [make_question(index) for index in range(1, 101)]
    session.add_all(questions)
    await session.flush()

    repo = CycleRepository(session, OWNER)
    claimed = []
    for _ in range(100):
        _, question, _ = await repo.claim_next_question(total_active=100)
        claimed.append(question.id)

    sequential = [question.id for question in questions]
    assert claimed != sequential, "claims came out in primary-key order"
    assert sorted(claimed) == sorted(sequential), "randomisation must not lose questions"


async def test_duplicate_claim_is_rejected_by_the_database(
    session: AsyncSession, question_bank
) -> None:
    """The unique constraint — not application logic — is what forbids a repeat."""
    repo = CycleRepository(session, OWNER)
    post, question, _ = await repo.claim_next_question(total_active=len(question_bank))

    duplicate = QuizPost(cycle_id=post.cycle_id, question_id=question.id)
    session.add(duplicate)

    with pytest.raises(IntegrityError):
        await session.flush()


async def test_inactive_questions_are_never_claimed(session: AsyncSession) -> None:
    """Soft-deleted questions stay out of the pool."""
    active = make_question(1)
    inactive = make_question(2)
    inactive.is_active = False
    session.add_all([active, inactive])
    await session.flush()

    repo = CycleRepository(session, OWNER)
    _, question, _ = await repo.claim_next_question(total_active=1)
    assert question.id == active.id

    # Only one active question exists, so the next claim must roll the cycle
    # rather than reach for the inactive one.
    _, second, rolled = await repo.claim_next_question(total_active=1)
    assert rolled is True
    assert second.id == active.id


async def test_empty_bank_raises(session: AsyncSession) -> None:
    """An empty bank is an operator problem and is reported as one."""
    repo = CycleRepository(session, OWNER)
    with pytest.raises(CycleExhaustedError):
        await repo.claim_next_question(total_active=0)


async def test_language_filter_restricts_the_pool(session: AsyncSession) -> None:
    """Only questions in the requested language are claimed."""
    uz = [make_question(index, language="uz") for index in range(1, 4)]
    ru = [make_question(index + 100, language="ru") for index in range(1, 4)]
    session.add_all([*uz, *ru])
    await session.flush()

    repo = CycleRepository(session, OWNER)
    for _ in range(3):
        _, question, _ = await repo.claim_next_question(language="ru", total_active=3)
        assert question.language == "ru"


async def test_remaining_count_tracks_progress(session: AsyncSession, question_bank) -> None:
    """``count_remaining`` decreases by one per claim."""
    repo = CycleRepository(session, OWNER)
    total = len(question_bank)

    cycle = await repo.get_or_create_open_cycle(total)
    assert await repo.count_remaining(cycle.id) == total

    for expected in range(total - 1, total - 6, -1):
        await repo.claim_next_question(total_active=total)
        assert await repo.count_remaining(cycle.id) == expected


async def test_questions_added_mid_cycle_become_available(
    session: AsyncSession, question_bank
) -> None:
    """An import during a cycle extends it rather than being deferred.

    Newly imported questions should reach the channel promptly; making them wait
    for the next cycle could delay them by weeks.
    """
    repo = CycleRepository(session, OWNER)
    total = len(question_bank)

    for _ in range(total):
        await repo.claim_next_question(total_active=total)

    cycle = await repo.get_open_cycle()
    assert cycle is not None
    assert await repo.count_remaining(cycle.id) == 0

    fresh = make_question(999)
    session.add(fresh)
    await session.flush()

    assert await repo.count_remaining(cycle.id) == 1
    _, question, rolled = await repo.claim_next_question(total_active=total + 1)
    assert rolled is False
    assert question.id == fresh.id


async def test_claimed_questions_survive_a_new_session(session: AsyncSession, engine) -> None:
    """Claims are durable, so a restart cannot resurrect a used question."""
    from sqlalchemy.ext.asyncio import async_sessionmaker

    questions = [make_question(index) for index in range(1, 6)]
    session.add_all(questions)
    await session.flush()

    repo = CycleRepository(session, OWNER)
    claimed = []
    for _ in range(3):
        _, question, _ = await repo.claim_next_question(total_active=5)
        claimed.append(question.id)
    await session.commit()

    # A brand-new session, as a restarted process would have.
    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as fresh_session:
        fresh_repo = CycleRepository(fresh_session, OWNER)
        cycle = await fresh_repo.get_open_cycle()
        assert cycle is not None
        assert await fresh_repo.count_posts_in_cycle(cycle.id) == 3

        for _ in range(2):
            _, question, _ = await fresh_repo.claim_next_question(total_active=5)
            assert question.id not in claimed, "a restart re-served an already-posted question"


async def test_active_question_count(session: AsyncSession, question_bank) -> None:
    """Sanity check on the bank fixture itself."""
    stored = list(await session.scalars(select(Question).where(Question.is_active.is_(True))))
    assert len(stored) == len(question_bank)
