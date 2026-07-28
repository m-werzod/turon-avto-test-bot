# syntax=docker/dockerfile:1.7
# =============================================================================
# Turon Avto Test | UZ — production image
#
# Two stages: wheels are built with a compiler present, then copied into a slim
# runtime that never ships gcc. That keeps the final image small and removes the
# build toolchain from the attack surface.
# =============================================================================

# --- Stage 1: build dependencies ---------------------------------------------
FROM python:3.12-slim-bookworm AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1

# asyncpg publishes wheels for CPython 3.12, but keep a compiler available so a
# source-only build of any transitive dependency still succeeds.
RUN apt-get update \
    && apt-get install --no-install-recommends -y build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build

# Copy only what pip needs to resolve dependencies, so the layer stays cached
# until the dependency list itself changes.
COPY pyproject.toml README.md ./
COPY bot/__init__.py ./bot/__init__.py

RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --upgrade pip setuptools wheel \
    && /opt/venv/bin/pip install .

# --- Stage 2: runtime ---------------------------------------------------------
FROM python:3.12-slim-bookworm AS runtime

LABEL org.opencontainers.image.title="Turon Avto Test Bot" \
      org.opencontainers.image.description="Telegram bot publishing Uzbek driving-exam quiz polls" \
      org.opencontainers.image.source="https://github.com/m-werzod/turon-avto-test-bot" \
      org.opencontainers.image.licenses="MIT"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONFAULTHANDLER=1 \
    PATH="/opt/venv/bin:$PATH" \
    TZ=Asia/Tashkent

# curl is used by the compose healthcheck; tini reaps zombies and forwards
# SIGTERM so the bot's graceful shutdown actually runs on `docker stop`.
RUN apt-get update \
    && apt-get install --no-install-recommends -y tini curl \
    && rm -rf /var/lib/apt/lists/*

# Run unprivileged: a compromised bot process should not be root in the container.
RUN groupadd --system --gid 1000 turon \
    && useradd --system --uid 1000 --gid turon --create-home turon

COPY --from=builder /opt/venv /opt/venv

WORKDIR /app

COPY --chown=turon:turon alembic.ini ./
COPY --chown=turon:turon alembic ./alembic
COPY --chown=turon:turon bot ./bot
COPY --chown=turon:turon data ./data
COPY --chown=turon:turon docker/entrypoint.sh /usr/local/bin/entrypoint.sh

RUN chmod +x /usr/local/bin/entrypoint.sh \
    && mkdir -p /app/logs /app/media/images /app/backups \
    && chown -R turon:turon /app

USER turon

# Fail fast on a broken image: if the package cannot even be imported, there is
# no point starting and looping through a restart policy.
HEALTHCHECK --interval=60s --timeout=10s --start-period=30s --retries=3 \
    CMD python -c "import bot; import sys; sys.exit(0)" || exit 1

ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/entrypoint.sh"]
CMD ["python", "-m", "bot"]
