"""Typed application settings.

Every value the application needs is declared here and sourced from the
environment (or a local ``.env`` file). Nothing is hardcoded, and the process
refuses to start when a required secret is missing or malformed — failing at
boot is far cheaper than failing at 08:00 in front of a channel audience.
"""

from __future__ import annotations

import sys
from functools import lru_cache
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, ValidationError, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

#: Repository root — ``bot/config/settings.py`` is three levels deep.
PROJECT_ROOT: Path = Path(__file__).resolve().parents[2]

LanguageCode = Literal["uz", "ru"]
LogFormat = Literal["text", "json"]
LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]


class Settings(BaseSettings):
    """Runtime configuration, validated once at start-up."""

    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- Telegram -------------------------------------------------------------
    bot_token: str = Field(alias="BOT_TOKEN", min_length=20)
    admin_ids_raw: str = Field(alias="ADMIN_IDS", default="")

    # --- Database -------------------------------------------------------------
    database_url: str = Field(alias="DATABASE_URL")

    # --- Scheduling -----------------------------------------------------------
    timezone_name: str = Field(alias="TIMEZONE", default="Asia/Tashkent")
    scheduler_misfire_grace: int = Field(alias="SCHEDULER_MISFIRE_GRACE", default=3600, ge=0)

    # --- Content --------------------------------------------------------------
    default_language: LanguageCode = Field(alias="DEFAULT_LANGUAGE", default="uz")
    media_root_raw: str = Field(alias="MEDIA_ROOT", default="media/images")
    send_images_as_document: bool = Field(alias="SEND_IMAGES_AS_DOCUMENT", default=False)
    brand_logo_raw: str = Field(alias="BRAND_LOGO", default="assets/logo.png")

    # --- Reliability ----------------------------------------------------------
    max_retries: int = Field(alias="MAX_RETRIES", default=3, ge=1, le=10)
    retry_backoff_seconds: float = Field(alias="RETRY_BACKOFF_SECONDS", default=2.0, gt=0)
    notify_admins_on_error: bool = Field(alias="NOTIFY_ADMINS_ON_ERROR", default=True)

    # --- Logging --------------------------------------------------------------
    log_level: LogLevel = Field(alias="LOG_LEVEL", default="INFO")
    log_dir_raw: str = Field(alias="LOG_DIR", default="logs")
    log_max_bytes: int = Field(alias="LOG_MAX_BYTES", default=10 * 1024 * 1024, ge=1024)
    log_backup_count: int = Field(alias="LOG_BACKUP_COUNT", default=10, ge=0)
    log_format: LogFormat = Field(alias="LOG_FORMAT", default="text")

    # --- Backups --------------------------------------------------------------
    backup_dir_raw: str = Field(alias="BACKUP_DIR", default="backups")

    # --- Validators -----------------------------------------------------------

    @field_validator("bot_token")
    @classmethod
    def _validate_bot_token(cls, value: str) -> str:
        """Reject the placeholder token shipped in ``.env.example``."""
        value = value.strip()
        if ":" not in value:
            raise ValueError("BOT_TOKEN must look like '<bot_id>:<secret>' (from @BotFather)")
        bot_id = value.split(":", 1)[0]
        if not bot_id.isdigit():
            raise ValueError("BOT_TOKEN must start with the numeric bot id")
        return value

    @field_validator("database_url")
    @classmethod
    def _validate_database_url(cls, value: str) -> str:
        """Ensure an *async* driver is configured — a sync DSN deadlocks the loop."""
        value = value.strip()
        allowed = ("postgresql+asyncpg://", "sqlite+aiosqlite://")
        if not value.startswith(allowed):
            raise ValueError(
                "DATABASE_URL must use an async driver, e.g. "
                "postgresql+asyncpg://user:pass@host:5432/dbname"
            )
        return value

    @field_validator("timezone_name")
    @classmethod
    def _validate_timezone(cls, value: str) -> str:
        value = value.strip()
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:  # pragma: no cover - env dependent
            raise ValueError(f"Unknown TIMEZONE {value!r}: {exc}") from exc
        return value

    # --- Derived values -------------------------------------------------------

    @property
    def admin_ids(self) -> frozenset[int]:
        """Telegram user ids permitted to open the admin panel.

        Parsed from a comma-separated string rather than a JSON list so operators
        can write ``ADMIN_IDS=1,2`` in ``.env`` without quoting rules.
        """
        ids: set[int] = set()
        for chunk in self.admin_ids_raw.replace(";", ",").split(","):
            chunk = chunk.strip()
            if not chunk:
                continue
            try:
                ids.add(int(chunk))
            except ValueError as exc:
                raise ValueError(f"ADMIN_IDS contains a non-numeric entry: {chunk!r}") from exc
        return frozenset(ids)

    @property
    def timezone(self) -> ZoneInfo:
        """Timezone every schedule and timestamp is rendered in."""
        return ZoneInfo(self.timezone_name)

    @property
    def media_root(self) -> Path:
        return self._resolve(self.media_root_raw)

    @property
    def brand_logo(self) -> Path:
        """Logo sent with the greeting. Absent is fine — greetings go out as text."""
        return self._resolve(self.brand_logo_raw)

    @property
    def log_dir(self) -> Path:
        return self._resolve(self.log_dir_raw)

    @property
    def backup_dir(self) -> Path:
        return self._resolve(self.backup_dir_raw)

    @staticmethod
    def _resolve(raw: str) -> Path:
        """Resolve a configured path, treating relative paths as repo-relative."""
        path = Path(raw).expanduser()
        return path if path.is_absolute() else PROJECT_ROOT / path

    def ensure_directories(self) -> None:
        """Create the writable directories the app assumes exist."""
        for path in (self.media_root, self.log_dir, self.backup_dir):
            path.mkdir(parents=True, exist_ok=True)


def database_url_only() -> str:
    """Resolve ``DATABASE_URL`` without validating anything else.

    Migrations need the DSN and nothing more. Building the full :class:`Settings`
    would also demand a valid ``BOT_TOKEN``, which would make
    ``alembic upgrade head`` fail during first-time setup and in any CI or
    maintenance context where no Telegram credentials exist — coupling that has
    no reason to be there.

    Precedence matches pydantic-settings: a real environment variable wins over
    the ``.env`` file.

    Returns:
        The configured DSN.

    Raises:
        RuntimeError: No DSN could be found in either place.
    """
    import os

    if from_environ := os.environ.get("DATABASE_URL", "").strip():
        return from_environ

    env_file = PROJECT_ROOT / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8", errors="replace").splitlines():
            stripped = line.strip()
            if stripped.startswith("#") or "=" not in stripped:
                continue
            key, _, value = stripped.partition("=")
            if key.strip() == "DATABASE_URL" and value.strip():
                return value.strip()

    raise RuntimeError(
        "DATABASE_URL is not set. Add it to .env or export it, e.g.\n"
        "  DATABASE_URL=sqlite+aiosqlite:///./turon.db"
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings singleton.

    Cached so that importing configuration from anywhere is free and always
    yields the same object.
    """
    return Settings()  # type: ignore[call-arg]  # values come from the environment


def load_settings_or_exit() -> Settings:
    """Load settings, printing an actionable message and exiting on failure.

    Used by the entrypoint: a misconfigured container should die loudly with a
    readable reason instead of dumping a raw pydantic traceback into the logs.
    """
    try:
        settings = get_settings()
    except ValidationError as exc:
        print("Configuration error — the bot cannot start:\n", file=sys.stderr)
        for error in exc.errors():
            field = error.get("loc", ("?",))[0]
            print(f"  * {field}: {error.get('msg')}", file=sys.stderr)
        print(
            "\nCopy .env.example to .env and fill in the required values.",
            file=sys.stderr,
        )
        raise SystemExit(2) from exc

    # admin_ids is a lazily-evaluated property, so a malformed value raises here
    # rather than during Settings() above and would otherwise escape as a raw
    # traceback — the exact thing this function exists to prevent.
    try:
        admin_ids = settings.admin_ids
    except ValueError as exc:
        print("Configuration error — the bot cannot start:\n", file=sys.stderr)
        print(f"  * ADMIN_IDS: {exc}", file=sys.stderr)
        print(
            "\nADMIN_IDS must be your numeric Telegram user id, not your @username.\n"
            "Message @userinfobot to get it, then set e.g. ADMIN_IDS=123456789\n"
            "(comma-separated for several admins).",
            file=sys.stderr,
        )
        raise SystemExit(2) from exc

    if not admin_ids:
        print("Configuration error — the bot cannot start:\n", file=sys.stderr)
        print(
            "  * ADMIN_IDS: empty, so nobody could open the admin panel.",
            file=sys.stderr,
        )
        print(
            "\nMessage @userinfobot to get your numeric Telegram id, then set\n"
            "ADMIN_IDS=123456789 in .env",
            file=sys.stderr,
        )
        raise SystemExit(2)

    settings.ensure_directories()
    return settings
