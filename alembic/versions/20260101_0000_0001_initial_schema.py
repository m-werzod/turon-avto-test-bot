"""Initial schema.

Creates the full set of tables: question bank, channels, cycles, posts,
deliveries, schedule slots, settings, users and the event log.

Revision ID: 0001
Revises:
Create Date: 2026-01-01 00:00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: JSONB on PostgreSQL, plain JSON elsewhere — mirrors bot.database.base.JSONType.
JSON_TYPE = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    # --- questions ------------------------------------------------------------
    op.create_table(
        "questions",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("external_id", sa.String(length=128), nullable=False),
        sa.Column("source", sa.String(length=64), nullable=False),
        sa.Column("language", sa.String(length=8), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("options", JSON_TYPE, nullable=False),
        sa.Column("correct_index", sa.Integer(), nullable=False),
        sa.Column("explanation", sa.Text(), nullable=True),
        sa.Column("category", sa.String(length=128), nullable=True),
        sa.Column("image_url", sa.Text(), nullable=True),
        sa.Column("image_path", sa.String(length=512), nullable=True),
        sa.Column("image_file_id", sa.String(length=256), nullable=True),
        sa.Column("original_url", sa.Text(), nullable=True),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default="true", nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "correct_index >= 0 AND correct_index < 4",
            name="ck_questions_correct_index_in_range",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_questions"),
    )
    op.create_index("ix_questions_source", "questions", ["source"])
    op.create_index("ix_questions_language", "questions", ["language"])
    op.create_index("ix_questions_category", "questions", ["category"])
    op.create_index("ix_questions_content_hash", "questions", ["content_hash"])
    op.create_index("ix_questions_is_active", "questions", ["is_active"])
    op.create_index("ix_questions_active_language", "questions", ["is_active", "language"])
    # The natural key. This is what makes re-importing idempotent.
    op.create_index(
        "uq_questions_source_external_id", "questions", ["source", "external_id"], unique=True
    )

    # --- channels -------------------------------------------------------------
    op.create_table(
        "channels",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("username", sa.String(length=64), nullable=True),
        sa.Column("title", sa.String(length=256), nullable=True),
        sa.Column("is_active", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("added_by", sa.BigInteger(), nullable=True),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", name="pk_channels"),
    )
    op.create_index("ix_channels_chat_id", "channels", ["chat_id"], unique=True)
    op.create_index("ix_channels_is_active", "channels", ["is_active"])

    # --- cycles ---------------------------------------------------------------
    op.create_table(
        "cycles",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("number", sa.Integer(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("questions_total", sa.Integer(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", name="pk_cycles"),
    )
    op.create_index("ix_cycles_number", "cycles", ["number"], unique=True)

    # --- quiz_posts -----------------------------------------------------------
    op.create_table(
        "quiz_posts",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("cycle_id", sa.Integer(), nullable=False),
        sa.Column("question_id", sa.Integer(), nullable=False),
        sa.Column(
            "trigger",
            sa.Enum(
                "scheduled",
                "manual",
                "catchup",
                name="posttrigger",
                native_enum=False,
                length=16,
            ),
            nullable=False,
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["cycle_id"], ["cycles.id"], name="fk_quiz_posts_cycle_id_cycles", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["question_id"],
            ["questions.id"],
            name="fk_quiz_posts_question_id_questions",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_quiz_posts"),
        # The no-repeat guarantee, enforced by the database rather than by
        # application logic that a race or a restart could sidestep.
        sa.UniqueConstraint("cycle_id", "question_id", name="uq_quiz_posts_cycle_question"),
    )
    op.create_index("ix_quiz_posts_cycle_id", "quiz_posts", ["cycle_id"])
    op.create_index("ix_quiz_posts_question_id", "quiz_posts", ["question_id"])
    op.create_index("ix_quiz_posts_cycle_created", "quiz_posts", ["cycle_id", "created_at"])

    # --- deliveries -----------------------------------------------------------
    op.create_table(
        "deliveries",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("post_id", sa.Integer(), nullable=False),
        sa.Column("channel_id", sa.Integer(), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "pending",
                "sent",
                "failed",
                name="deliverystatus",
                native_enum=False,
                length=16,
            ),
            nullable=False,
        ),
        sa.Column("poll_message_id", sa.BigInteger(), nullable=True),
        sa.Column("photo_message_id", sa.BigInteger(), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["post_id"],
            ["quiz_posts.id"],
            name="fk_deliveries_post_id_quiz_posts",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["channel_id"],
            ["channels.id"],
            name="fk_deliveries_channel_id_channels",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_deliveries"),
    )
    op.create_index("ix_deliveries_post_id", "deliveries", ["post_id"])
    op.create_index("ix_deliveries_channel_id", "deliveries", ["channel_id"])
    op.create_index("ix_deliveries_status", "deliveries", ["status"])
    op.create_index("ix_deliveries_status_sent_at", "deliveries", ["status", "sent_at"])

    # --- schedule_slots -------------------------------------------------------
    op.create_table(
        "schedule_slots",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("run_at", sa.Time(), nullable=False),
        sa.Column("is_enabled", sa.Boolean(), server_default="true", nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", name="pk_schedule_slots"),
        sa.UniqueConstraint("run_at", name="uq_schedule_slots_run_at"),
    )
    op.create_index("ix_schedule_slots_enabled_run_at", "schedule_slots", ["is_enabled", "run_at"])

    # --- settings -------------------------------------------------------------
    op.create_table(
        "settings",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column("value", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", name="pk_settings"),
    )
    op.create_index("ix_settings_key", "settings", ["key"], unique=True)

    # --- bot_users ------------------------------------------------------------
    op.create_table(
        "bot_users",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("telegram_id", sa.BigInteger(), nullable=False),
        sa.Column("username", sa.String(length=64), nullable=True),
        sa.Column("first_name", sa.String(length=128), nullable=True),
        sa.Column("last_name", sa.String(length=128), nullable=True),
        sa.Column("language", sa.String(length=8), nullable=False),
        sa.Column("is_admin", sa.Boolean(), server_default="false", nullable=False),
        sa.Column(
            "last_seen_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", name="pk_bot_users"),
    )
    op.create_index("ix_bot_users_telegram_id", "bot_users", ["telegram_id"], unique=True)

    # --- event_logs -----------------------------------------------------------
    op.create_table(
        "event_logs",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("level", sa.String(length=16), nullable=False),
        sa.Column("event", sa.String(length=64), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("payload", JSON_TYPE, nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", name="pk_event_logs"),
    )
    op.create_index("ix_event_logs_level", "event_logs", ["level"])
    op.create_index("ix_event_logs_event", "event_logs", ["event"])
    op.create_index("ix_event_logs_created_desc", "event_logs", ["created_at"])
    op.create_index("ix_event_logs_level_created", "event_logs", ["level", "created_at"])


def downgrade() -> None:
    # Reverse dependency order so foreign keys never block a drop.
    op.drop_table("event_logs")
    op.drop_table("bot_users")
    op.drop_table("settings")
    op.drop_table("schedule_slots")
    op.drop_table("deliveries")
    op.drop_table("quiz_posts")
    op.drop_table("cycles")
    op.drop_table("channels")
    op.drop_table("questions")
