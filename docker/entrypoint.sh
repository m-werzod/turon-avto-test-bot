#!/bin/sh
# =============================================================================
# Container entrypoint.
#
# Waits for PostgreSQL, applies migrations, then hands over to the bot. Running
# migrations here rather than in a separate compose service means a fresh
# `docker compose up` on an empty volume just works, and an upgrade that adds a
# migration applies it on the next restart without a manual step.
# =============================================================================
set -eu

DB_WAIT_TIMEOUT="${DB_WAIT_TIMEOUT:-60}"

log() {
    printf '%s | entrypoint | %s\n' "$(date -u '+%Y-%m-%d %H:%M:%S')" "$*"
}

# --- Wait for the database ---------------------------------------------------
# The bot's own healthcheck would also catch this, but failing here produces a
# far clearer message than a stack trace from the first query.
wait_for_database() {
    log "Waiting for the database (timeout ${DB_WAIT_TIMEOUT}s)…"

    elapsed=0
    while [ "$elapsed" -lt "$DB_WAIT_TIMEOUT" ]; do
        if python - <<'PY'
import asyncio, os, sys

async def check() -> int:
    url = os.environ.get("DATABASE_URL", "")
    if url.startswith("sqlite"):
        return 0
    try:
        from sqlalchemy import text
        from sqlalchemy.ext.asyncio import create_async_engine

        engine = create_async_engine(url)
        try:
            async with engine.connect() as connection:
                await connection.execute(text("SELECT 1"))
        finally:
            await engine.dispose()
    except Exception:
        return 1
    return 0

sys.exit(asyncio.run(check()))
PY
        then
            log "Database is ready."
            return 0
        fi

        elapsed=$((elapsed + 2))
        sleep 2
    done

    log "ERROR: the database did not become reachable within ${DB_WAIT_TIMEOUT}s."
    log "Check DATABASE_URL and that the postgres service is healthy."
    return 1
}

# --- Apply migrations --------------------------------------------------------
apply_migrations() {
    log "Applying database migrations…"
    if alembic upgrade head; then
        log "Migrations are up to date."
    else
        log "ERROR: migrations failed. Refusing to start against an unknown schema."
        return 1
    fi
}

wait_for_database
apply_migrations

log "Starting: $*"
exec "$@"
