"""Alembic environment, wired to the app's async engine and settings."""

from __future__ import annotations

import asyncio
from logging.config import fileConfig

from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from alembic import context
from bot.config.settings import database_url_only

# Importing the models package registers every table on Base.metadata.
# Without this import autogenerate would see an empty schema.
from bot.database import models  # noqa: F401
from bot.database.base import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _database_url() -> str:
    """Resolve the DSN from the environment rather than alembic.ini.

    Deliberately reads only DATABASE_URL. Migrations must not require a valid
    BOT_TOKEN — that would break first-time setup, CI and maintenance runs where
    no Telegram credentials exist.
    """
    return database_url_only()


def run_migrations_offline() -> None:
    """Emit SQL to stdout without connecting.

    Useful for reviewing a migration or handing DDL to a DBA.
    """
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def _do_run_migrations(connection: Connection) -> None:
    """Run migrations on an established synchronous connection."""
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        compare_server_default=True,
        # Keep the naming convention available to autogenerate so constraints
        # are emitted with the same names the models declare.
        render_as_batch=connection.dialect.name == "sqlite",
    )
    with context.begin_transaction():
        context.run_migrations()


async def _run_async_migrations() -> None:
    """Create an async engine and run migrations through it."""
    configuration = config.get_section(config.config_ini_section, {})
    configuration["sqlalchemy.url"] = _database_url()

    connectable = async_engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(_do_run_migrations)

    await connectable.dispose()


def run_migrations_online() -> None:
    """Entry point for a normal ``alembic upgrade``."""
    asyncio.run(_run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
