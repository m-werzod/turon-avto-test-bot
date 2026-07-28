"""multi-tenant: owner_id on channels, cycles, slots, settings

The bot was built for one operator: a single channel list, one schedule, one
cycle, one settings row per key. Anyone can now connect their own channel and run
their own schedule over the shared question bank, so every row that belongs to a
person rather than to the installation gains an owner.

The columns are added nullable, backfilled, and only then made NOT NULL —
declaring NOT NULL up front would fail against any existing row, which is exactly
the deployment this migration exists for.

Backfill target is the first id in ADMIN_IDS: on an existing install every
channel, slot and cycle belongs to whoever has been running it. Read from the
environment because that is where the answer lives; a hardcoded id would be wrong
on every deployment but the one it was written on.

``settings`` keeps a nullable owner, where NULL means installation-wide. Only the
import bookkeeping stays global — the question bank is shared, so "when was it
last refreshed" is one fact for everybody. Everything else (pause, batch size,
content language, weekend skipping, cycle pointer) describes one operator's
choices and moves to them.

Revision ID: 0652c36e725c
Revises: 9eef38ae0e44
Create Date: 2026-07-28 17:48:03.695185
"""

from __future__ import annotations

import os
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0652c36e725c"
down_revision: str | None = "9eef38ae0e44"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Settings that describe the shared question bank rather than one operator.
GLOBAL_SETTING_KEYS = ("last_import_at", "last_import_summary")


def _existing_owner() -> int:
    """Telegram id to attribute pre-existing rows to.

    Returns:
        The first configured admin id, or ``0`` when none is set — a sentinel
        that keeps the migration runnable on an empty database rather than
        failing a fresh install for want of an environment variable.
    """
    raw = os.environ.get("ADMIN_IDS", "")
    if not raw:
        env_file = os.path.join(os.getcwd(), ".env")
        if os.path.exists(env_file):
            with open(env_file, encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    stripped = line.strip()
                    if stripped.startswith("ADMIN_IDS="):
                        raw = stripped.split("=", 1)[1]
                        break

    for chunk in raw.replace(";", ",").split(","):
        chunk = chunk.strip()
        if chunk.isdigit():
            return int(chunk)
    return 0


def upgrade() -> None:
    owner = _existing_owner()

    # --- add nullable ---------------------------------------------------------
    for table in ("channels", "cycles", "schedule_slots"):
        with op.batch_alter_table(table, schema=None) as batch_op:
            batch_op.add_column(sa.Column("owner_id", sa.BigInteger(), nullable=True))

    with op.batch_alter_table("settings", schema=None) as batch_op:
        batch_op.add_column(sa.Column("owner_id", sa.BigInteger(), nullable=True))

    # --- backfill -------------------------------------------------------------
    # A channel records who added it; prefer that over the blanket admin id so a
    # co-admin keeps the channels they personally connected.
    op.execute(
        sa.text(
            "UPDATE channels SET owner_id = COALESCE(added_by, :owner) WHERE owner_id IS NULL"
        ).bindparams(owner=owner)
    )
    for table in ("cycles", "schedule_slots"):
        op.execute(
            sa.text(f"UPDATE {table} SET owner_id = :owner WHERE owner_id IS NULL").bindparams(
                owner=owner
            )
        )

    placeholders = ", ".join(f"'{key}'" for key in GLOBAL_SETTING_KEYS)
    op.execute(
        sa.text(
            f"UPDATE settings SET owner_id = :owner WHERE key NOT IN ({placeholders})"
        ).bindparams(owner=owner)
    )

    # --- tighten and index ----------------------------------------------------
    with op.batch_alter_table("channels", schema=None) as batch_op:
        batch_op.alter_column("owner_id", existing_type=sa.BigInteger(), nullable=False)
        batch_op.create_index(batch_op.f("ix_channels_owner_id"), ["owner_id"], unique=False)

    with op.batch_alter_table("cycles", schema=None) as batch_op:
        batch_op.alter_column("owner_id", existing_type=sa.BigInteger(), nullable=False)
        batch_op.drop_index(batch_op.f("ix_cycles_number"))
        batch_op.create_index(batch_op.f("ix_cycles_number"), ["number"], unique=False)
        batch_op.create_index(batch_op.f("ix_cycles_owner_id"), ["owner_id"], unique=False)
        batch_op.create_unique_constraint("owner_cycle_number", ["owner_id", "number"])

    with op.batch_alter_table("schedule_slots", schema=None) as batch_op:
        batch_op.alter_column("owner_id", existing_type=sa.BigInteger(), nullable=False)
        batch_op.drop_constraint(batch_op.f("uq_schedule_slots_run_at"), type_="unique")
        batch_op.create_index(batch_op.f("ix_schedule_slots_owner_id"), ["owner_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_schedule_slots_run_at"), ["run_at"], unique=False)
        batch_op.create_unique_constraint("owner_slot_time", ["owner_id", "run_at"])

    with op.batch_alter_table("settings", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_settings_key"))
        batch_op.create_index(batch_op.f("ix_settings_key"), ["key"], unique=False)
        batch_op.create_index(batch_op.f("ix_settings_owner_id"), ["owner_id"], unique=False)
        batch_op.create_unique_constraint("owner_setting_key", ["owner_id", "key"])


def downgrade() -> None:
    # Only one owner's rows can survive a return to single-tenant storage; the
    # rest would collide on the restored global unique constraints.
    owner = _existing_owner()
    op.execute(sa.text("DELETE FROM channels WHERE owner_id <> :owner").bindparams(owner=owner))
    op.execute(
        sa.text("DELETE FROM schedule_slots WHERE owner_id <> :owner").bindparams(owner=owner)
    )
    op.execute(
        sa.text(
            "DELETE FROM quiz_posts WHERE cycle_id IN "
            "(SELECT id FROM cycles WHERE owner_id <> :owner)"
        ).bindparams(owner=owner)
    )
    op.execute(sa.text("DELETE FROM cycles WHERE owner_id <> :owner").bindparams(owner=owner))
    op.execute(
        sa.text(
            "DELETE FROM settings WHERE owner_id IS NOT NULL AND owner_id <> :owner"
        ).bindparams(owner=owner)
    )

    with op.batch_alter_table("settings", schema=None) as batch_op:
        batch_op.drop_constraint("owner_setting_key", type_="unique")
        batch_op.drop_index(batch_op.f("ix_settings_owner_id"))
        batch_op.drop_index(batch_op.f("ix_settings_key"))
        batch_op.create_index(batch_op.f("ix_settings_key"), ["key"], unique=1)
        batch_op.drop_column("owner_id")

    with op.batch_alter_table("schedule_slots", schema=None) as batch_op:
        batch_op.drop_constraint("owner_slot_time", type_="unique")
        batch_op.drop_index(batch_op.f("ix_schedule_slots_run_at"))
        batch_op.drop_index(batch_op.f("ix_schedule_slots_owner_id"))
        batch_op.create_unique_constraint(batch_op.f("uq_schedule_slots_run_at"), ["run_at"])
        batch_op.drop_column("owner_id")

    with op.batch_alter_table("cycles", schema=None) as batch_op:
        batch_op.drop_constraint("owner_cycle_number", type_="unique")
        batch_op.drop_index(batch_op.f("ix_cycles_owner_id"))
        batch_op.drop_index(batch_op.f("ix_cycles_number"))
        batch_op.create_index(batch_op.f("ix_cycles_number"), ["number"], unique=1)
        batch_op.drop_column("owner_id")

    with op.batch_alter_table("channels", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_channels_owner_id"))
        batch_op.drop_column("owner_id")
