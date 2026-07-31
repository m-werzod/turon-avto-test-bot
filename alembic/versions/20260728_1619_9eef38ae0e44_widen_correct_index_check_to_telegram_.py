"""widen correct_index check to Telegram poll limit

The original constraint pinned ``correct_index`` below 4, encoding an assumption
that every question has exactly four options. The official Uzbek banks do not
work that way — questions carry between two and five options, and only about one
in seven has precisely four — so importing real data failed on the constraint.
The bound is now Telegram's actual quiz-poll limit of ten.

Autogenerate does not compare CHECK constraints, so this is written by hand.

Neither is it written with ``batch_alter_table``. SQLAlchemy cannot reliably
reflect named CHECK constraints out of SQLite, and batch mode additionally
re-applies the naming convention to whatever names it does recover — so the drop
target either goes missing or acquires a second ``ck_questions_`` prefix,
depending on which spelling is passed. Both dialects are therefore handled
explicitly: PostgreSQL takes a plain ALTER, and SQLite gets the same table
rebuild batch mode would have performed, driven by its own stored DDL instead of
by reflection.

That stored DDL also explains the doubled name below: 0001 created the constraint
as ``ck_questions_ck_questions_correct_index_in_range``, the naming convention
having been applied to a name already rendered through it. The rebuild spells it
correctly, bringing the database in line with the model.

Revision ID: 9eef38ae0e44
Revises: 0001
Create Date: 2026-07-28 16:19:21.298331
"""

from __future__ import annotations

import re
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "9eef38ae0e44"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: What 0001 actually created, doubled prefix and all.
OLD_CONSTRAINT = "ck_questions_ck_questions_correct_index_in_range"

#: Convention-rendered name, matching what the model declares.
NEW_CONSTRAINT = "ck_questions_correct_index_in_range"


def _rebuild_sqlite(*, from_bound: int, to_bound: int, from_name: str, to_name: str) -> None:
    """Recreate ``questions`` with a different CHECK bound.

    Works from the DDL SQLite already stores, so the rebuilt table keeps every
    column, type and default exactly as it was — no column list is duplicated
    here to drift out of step with the model.
    """
    connection = op.get_bind()
    ddl = connection.execute(
        sa.text("SELECT sql FROM sqlite_master WHERE type='table' AND name='questions'")
    ).scalar()
    if not ddl:
        raise RuntimeError("questions table not found")

    rebuilt = ddl.replace(
        f"CONSTRAINT {from_name} CHECK (correct_index >= 0 AND correct_index < {from_bound})",
        f"CONSTRAINT {to_name} CHECK (correct_index >= 0 AND correct_index < {to_bound})",
    )
    if rebuilt == ddl:
        # Fall back to matching the bound alone, in case the constraint carries a
        # name this migration does not predict.
        rebuilt = re.sub(
            rf"correct_index\s*<\s*{from_bound}\b",
            f"correct_index < {to_bound}",
            ddl,
            count=1,
        )
    if rebuilt == ddl:
        raise RuntimeError(f"Could not find the correct_index CHECK constraint in: {ddl}")

    rebuilt = rebuilt.replace("CREATE TABLE questions", "CREATE TABLE questions_new", 1)
    rebuilt = rebuilt.replace('CREATE TABLE "questions"', "CREATE TABLE questions_new", 1)

    # Indexes live outside the table DDL and go with it, so they are recreated
    # from their own stored definitions afterwards.
    index_ddl = [
        row[0]
        for row in connection.execute(
            sa.text(
                "SELECT sql FROM sqlite_master WHERE type='index' "
                "AND tbl_name='questions' AND sql IS NOT NULL"
            )
        )
    ]

    op.execute(sa.text(rebuilt))
    op.execute(sa.text("INSERT INTO questions_new SELECT * FROM questions"))
    op.execute(sa.text("DROP TABLE questions"))
    op.execute(sa.text("ALTER TABLE questions_new RENAME TO questions"))
    for statement in index_ddl:
        op.execute(sa.text(statement))


def upgrade() -> None:
    if op.get_bind().dialect.name == "sqlite":
        _rebuild_sqlite(from_bound=4, to_bound=10, from_name=OLD_CONSTRAINT, to_name=NEW_CONSTRAINT)
        return

    op.drop_constraint(op.f(OLD_CONSTRAINT), "questions", type_="check")
    op.create_check_constraint(
        op.f(NEW_CONSTRAINT), "questions", "correct_index >= 0 AND correct_index < 10"
    )


def downgrade() -> None:
    # Rows with an index of 4 or more cannot satisfy the old constraint, so they
    # go first; leaving them would make the downgrade fail outright.
    op.execute(sa.text("DELETE FROM questions WHERE correct_index >= 4"))

    if op.get_bind().dialect.name == "sqlite":
        _rebuild_sqlite(from_bound=10, to_bound=4, from_name=NEW_CONSTRAINT, to_name=OLD_CONSTRAINT)
        return

    op.drop_constraint(op.f(NEW_CONSTRAINT), "questions", type_="check")
    op.create_check_constraint(
        op.f(OLD_CONSTRAINT), "questions", "correct_index >= 0 AND correct_index < 4"
    )
