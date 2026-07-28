"""Minimal, dependency-free translation layer.

Translations live in flat JSON files keyed by dotted names. A tiny loader is
enough here and avoids the compile step and binary ``.mo`` artefacts that gettext
would add to the Docker image for two languages.

Missing keys never raise: a caller gets the key back and a warning is logged, so
an untranslated string degrades to something visible rather than a 500 in a
handler that was about to answer an admin.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Final

from bot.utils.logging import get_logger

logger = get_logger(__name__)

LOCALES_DIR: Final[Path] = Path(__file__).resolve().parent

#: Languages the bot ships with.
SUPPORTED_LANGUAGES: Final[tuple[str, ...]] = ("uz", "ru")

#: Used when a user has no stored preference and for missing-key fallback.
FALLBACK_LANGUAGE: Final[str] = "uz"

#: Human labels for the language picker.
LANGUAGE_LABELS: Final[dict[str, str]] = {
    "uz": "🇺🇿 O'zbekcha",
    "ru": "🇷🇺 Русский",
}


class Translator:
    """Loads locale files and resolves dotted keys."""

    def __init__(self, locales_dir: Path = LOCALES_DIR) -> None:
        self._locales_dir = locales_dir
        self._catalogs: dict[str, dict[str, Any]] = {}
        self._warned_missing: set[str] = set()
        self.reload()

    def reload(self) -> None:
        """Load every supported locale from disk.

        Raises:
            FileNotFoundError: A supported language has no JSON file. This is a
                packaging error and must fail loudly at start-up.
        """
        catalogs: dict[str, dict[str, Any]] = {}
        for language in SUPPORTED_LANGUAGES:
            path = self._locales_dir / f"{language}.json"
            if not path.exists():
                raise FileNotFoundError(f"Missing locale file: {path}")
            with path.open(encoding="utf-8") as handle:
                catalogs[language] = json.load(handle)
        self._catalogs = catalogs
        logger.info("Loaded %d locale(s): %s", len(catalogs), ", ".join(catalogs))

    def normalize(self, language: str | None) -> str:
        """Map anything user- or Telegram-supplied onto a supported code.

        Telegram sends codes like ``ru-RU`` or ``uz-Latn``; only the primary
        subtag matters here.
        """
        if not language:
            return FALLBACK_LANGUAGE
        primary = language.split("-", 1)[0].lower()
        return primary if primary in SUPPORTED_LANGUAGES else FALLBACK_LANGUAGE

    def _lookup(self, catalog: dict[str, Any], key: str) -> str | None:
        """Walk a dotted key through nested dicts."""
        node: Any = catalog
        for part in key.split("."):
            if not isinstance(node, dict) or part not in node:
                return None
            node = node[part]
        return node if isinstance(node, str) else None

    def get(self, key: str, language: str | None = None, /, **params: Any) -> str:
        """Translate ``key`` into ``language``.

        Args:
            key: Dotted key, e.g. ``"menu.statistics"``.
            language: Target language; falls back when unknown or missing.
            **params: Values interpolated with ``str.format``.

        Returns:
            The translated string, or the key itself if nothing was found.
        """
        lang = self.normalize(language)
        text = self._lookup(self._catalogs.get(lang, {}), key)

        if text is None and lang != FALLBACK_LANGUAGE:
            text = self._lookup(self._catalogs.get(FALLBACK_LANGUAGE, {}), key)

        if text is None:
            # Warn once per key so a missing string is reported without flooding
            # the log on every single update.
            if key not in self._warned_missing:
                self._warned_missing.add(key)
                logger.warning("Missing translation for key %r (language=%s)", key, lang)
            return key

        if not params:
            return text
        try:
            return text.format(**params)
        except (KeyError, IndexError, ValueError) as exc:
            logger.error("Bad format parameters for key %r: %s", key, exc)
            return text


#: Process-wide translator. Locale files are static, so one instance is enough.
translator = Translator()


def t(key: str, language: str | None = None, /, **params: Any) -> str:
    """Shorthand for :meth:`Translator.get` on the shared translator."""
    return translator.get(key, language, **params)
