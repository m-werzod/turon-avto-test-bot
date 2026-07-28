"""Shared pytest fixtures.

Tests run against a real SQLite database rather than mocks, so the constraints
and queries that carry the important behaviour — the unique index behind the
no-repeat rule, ``ORDER BY random()``, savepoint rollback — are genuinely
exercised rather than assumed.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from pathlib import Path

import pytest_asyncio
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

# Settings are constructed at import time in several modules, so the environment
# must be complete before anything from `bot` is imported.
os.environ.setdefault("BOT_TOKEN", "123456789:TEST-TOKEN-FOR-THE-TEST-SUITE")
os.environ.setdefault("ADMIN_IDS", "1001,1002")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
os.environ.setdefault("TIMEZONE", "Asia/Tashkent")

from bot.database import models  # noqa: F401  — registers every table
from bot.database.base import Base
from bot.database.models.question import Question


@pytest_asyncio.fixture
async def engine(tmp_path: Path) -> AsyncIterator[object]:
    """A fresh SQLite engine with working SAVEPOINT support.

    pysqlite (and therefore aiosqlite) emits its own implicit BEGIN, which breaks
    nested transactions. The two listeners below are SQLAlchemy's documented
    workaround; without them ``begin_nested`` silently does nothing and the
    concurrent-claim test would pass for the wrong reason.
    """
    url = f"sqlite+aiosqlite:///{tmp_path / 'test.db'}"
    engine = create_async_engine(url, future=True)

    @event.listens_for(engine.sync_engine, "connect")
    def _disable_implicit_begin(dbapi_connection, _record):  # type: ignore[no-untyped-def]
        dbapi_connection.isolation_level = None

    @event.listens_for(engine.sync_engine, "begin")
    def _emit_begin(conn):  # type: ignore[no-untyped-def]
        conn.exec_driver_sql("BEGIN")

    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def session(engine) -> AsyncIterator[AsyncSession]:  # type: ignore[no-untyped-def]
    """An open session bound to the test database."""
    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        yield session


def make_question(index: int, *, language: str = "uz", source: str = "test") -> Question:
    """Build a valid question for tests.

    Args:
        index: Used for the external id and to vary the text.
        language: Content language.
        source: Source identity.

    Returns:
        An unsaved question.
    """
    options = [f"Option A{index}", f"Option B{index}", f"Option C{index}", f"Option D{index}"]
    correct = index % 4
    text = f"Test question number {index}?"
    return Question(
        source=source,
        external_id=str(index),
        text=text,
        options=options,
        correct_index=correct,
        explanation=f"Explanation {index}",
        language=language,
        content_hash=Question.compute_content_hash(text, options, correct, f"Explanation {index}"),
        is_active=True,
    )


@pytest_asyncio.fixture
async def question_bank(session: AsyncSession) -> list[Question]:
    """Twenty-five stored questions."""
    questions = [make_question(index) for index in range(1, 26)]
    session.add_all(questions)
    await session.flush()
    return questions
