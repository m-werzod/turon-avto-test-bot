"""Cycle bookkeeping and the "claim the next question" operation.

This module implements the core content rule: a question is never posted twice
until the entire bank has been used, after which a fresh cycle starts by itself.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from bot.database.models.cycle import Cycle
from bot.database.models.question import Question
from bot.database.models.quiz_post import PostTrigger, QuizPost
from bot.database.repositories.base import OwnedRepository
from bot.utils.logging import get_logger

logger = get_logger(__name__)

#: How many times to retry a claim that lost a race to a concurrent claim.
_CLAIM_ATTEMPTS = 5


class CycleExhaustedError(RuntimeError):
    """Raised when no question can be claimed at all.

    In practice this means the question bank is empty (or every question is
    inactive), which is an operator problem, not a transient one.
    """


class CycleRepository(OwnedRepository[Cycle]):
    """Cycles, and the allocation of questions to them."""

    model = Cycle

    async def get_open_cycle(self) -> Cycle | None:
        """The cycle currently accepting posts, if one is open."""
        stmt = (
            self.owned(select(Cycle).where(Cycle.completed_at.is_(None)))
            .order_by(Cycle.number.desc())
            .limit(1)
        )
        return await self.session.scalar(stmt)

    async def get_or_create_open_cycle(self, questions_total: int = 0) -> Cycle:
        """Return the open cycle, opening the first one if none exists."""
        cycle = await self.get_open_cycle()
        if cycle is not None:
            return cycle
        return await self.open_next_cycle(questions_total)

    async def open_next_cycle(self, questions_total: int = 0) -> Cycle:
        """Open a new cycle, numbered one above this owner's highest so far.

        Scoped to the owner: cycle numbers are what the panel shows as "cycle
        #3", so a user who joined late should start at 1, not inherit a count
        from everyone who came before them.
        """
        highest = await self.session.scalar(self.owned(select(func.max(Cycle.number))))
        cycle = Cycle(
            number=int(highest or 0) + 1,
            started_at=datetime.now(UTC),
            questions_total=questions_total,
        )
        await self.add(cycle)
        logger.info(
            "Cycle %d started with %d question(s)",
            cycle.number,
            questions_total,
            extra={"cycle": cycle.number, "questions_total": questions_total},
        )
        return cycle

    async def close_cycle(self, cycle: Cycle) -> None:
        """Mark a cycle finished."""
        if cycle.completed_at is None:
            cycle.completed_at = datetime.now(UTC)
            await self.session.flush()
            logger.info("Cycle %d completed", cycle.number, extra={"cycle": cycle.number})

    async def count_posts_in_cycle(self, cycle_id: int) -> int:
        """How many questions have been used in a cycle."""
        stmt = select(func.count()).select_from(QuizPost).where(QuizPost.cycle_id == cycle_id)
        return int(await self.session.scalar(stmt) or 0)

    async def count_remaining(self, cycle_id: int, language: str | None = None) -> int:
        """Active questions not yet used in this cycle."""
        used = select(QuizPost.question_id).where(QuizPost.cycle_id == cycle_id)
        stmt = (
            select(func.count())
            .select_from(Question)
            .where(Question.is_active.is_(True), Question.id.not_in(used))
        )
        if language:
            stmt = stmt.where(Question.language == language)
        return int(await self.session.scalar(stmt) or 0)

    async def _pick_unused_question(self, cycle_id: int, language: str | None) -> Question | None:
        """Choose one random active question unused in this cycle.

        Randomisation happens in the database rather than by loading ids into
        Python, so memory stays flat regardless of bank size.
        """
        used = select(QuizPost.question_id).where(QuizPost.cycle_id == cycle_id)
        stmt = (
            select(Question)
            .where(Question.is_active.is_(True), Question.id.not_in(used))
            .order_by(func.random())
            .limit(1)
        )
        if language:
            stmt = stmt.where(Question.language == language)
        return await self.session.scalar(stmt)

    async def claim_next_question(
        self,
        *,
        trigger: PostTrigger = PostTrigger.SCHEDULED,
        language: str | None = None,
        total_active: int | None = None,
    ) -> tuple[QuizPost, Question, bool]:
        """Reserve the next question to publish.

        Picks a random question that has not appeared in the current cycle and
        records the claim. When the cycle has no unused questions left it is
        closed and a new one is opened automatically, so posting never stops.

        The insert can collide with a concurrent claim — a manual "Send now"
        racing the 13:00 job, for example. The unique constraint on
        ``(cycle_id, question_id)`` turns that into an IntegrityError rather than
        a duplicate post, and we simply pick again.

        Args:
            trigger: What caused this post.
            language: Restrict to one content language, or ``None`` for any.
            total_active: Pre-computed size of the active bank, recorded on a
                newly opened cycle. Queried when omitted.

        Returns:
            The claim, the question to publish, and whether a new cycle was
            opened as part of this call.

        Raises:
            CycleExhaustedError: No question could be claimed — an empty bank, or
                persistent contention.
        """
        cycle = await self.get_or_create_open_cycle(total_active or 0)
        cycle_rolled = False

        for attempt in range(1, _CLAIM_ATTEMPTS + 1):
            question = await self._pick_unused_question(cycle.id, language)

            if question is None:
                # Everything in this cycle has been used. Close it and roll over.
                if cycle_rolled:
                    # Already rolled once this call and the fresh cycle is empty
                    # too — the bank itself has nothing to offer.
                    raise CycleExhaustedError(
                        "No active questions available to post. Import a question "
                        "bank before scheduling."
                    )
                await self.close_cycle(cycle)
                active_count = total_active
                if active_count is None:
                    active_count = int(
                        await self.session.scalar(
                            select(func.count())
                            .select_from(Question)
                            .where(Question.is_active.is_(True))
                        )
                        or 0
                    )
                cycle = await self.open_next_cycle(active_count)
                cycle_rolled = True
                continue

            post = QuizPost(cycle_id=cycle.id, question_id=question.id, trigger=trigger)
            self.session.add(post)
            try:
                # Flush inside a savepoint: a collision must not poison the outer
                # transaction that the caller is still using.
                async with self.session.begin_nested():
                    await self.session.flush()
            except IntegrityError:
                logger.warning(
                    "Question %d was claimed concurrently in cycle %d; retrying (%d/%d)",
                    question.id,
                    cycle.id,
                    attempt,
                    _CLAIM_ATTEMPTS,
                )
                continue

            logger.info(
                "Claimed question %d for cycle %d (trigger=%s)",
                question.id,
                cycle.number,
                trigger.value,
                extra={
                    "question_id": question.id,
                    "cycle": cycle.number,
                    "trigger": trigger.value,
                },
            )
            return post, question, cycle_rolled

        raise CycleExhaustedError(
            f"Could not claim a question after {_CLAIM_ATTEMPTS} attempts due to contention."
        )

    async def recent_cycles(self, limit: int = 5) -> list[Cycle]:
        """Most recent cycles, newest first."""
        stmt = self.owned(select(Cycle)).order_by(Cycle.number.desc()).limit(limit)
        result = await self.session.scalars(stmt)
        return list(result)
