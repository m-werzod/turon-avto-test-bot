"""Text helpers, retry policy and localization."""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

import pytest

from bot.locales.i18n import SUPPORTED_LANGUAGES, Translator, t
from bot.utils.retry import RetryError, retry_async
from bot.utils.text import (
    POLL_QUESTION_LIMIT,
    collapse_whitespace,
    normalize_channel_identifier,
    truncate,
)


class TestTruncate:
    """Clamping text to Bot API limits."""

    def test_short_text_untouched(self) -> None:
        assert truncate("hello", 100) == "hello"

    def test_result_never_exceeds_the_limit(self) -> None:
        for limit in (1, 5, 20, 100, POLL_QUESTION_LIMIT):
            assert len(truncate("word " * 200, limit)) <= limit

    def test_breaks_on_a_word_boundary_when_it_is_cheap(self) -> None:
        """A nearby space is preferred over cutting mid-word."""
        assert truncate("alpha beta gamma delta", 14) == "alpha beta…"

    def test_keeps_a_partial_word_rather_than_losing_a_third_of_the_budget(self) -> None:
        """The word break only applies inside the last 20% of the budget.

        At limit 16 the usable budget is 15 and the last space sits at index 10,
        so breaking there would throw away a third of the allowance. Keeping the
        partial word carries more information than a much shorter clean cut.
        """
        assert truncate("alpha beta gamma delta", 16) == "alpha beta gamm…"

    def test_zero_limit(self) -> None:
        assert truncate("anything", 0) == ""


class TestCollapseWhitespace:
    """Poll fields render on one line, so newlines must go."""

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("a  b", "a b"),
            ("a\nb", "a b"),
            ("a\t\tb", "a b"),
            ("  padded  ", "padded"),
            ("multi\n\n\nline", "multi line"),
        ],
    )
    def test_collapses(self, raw: str, expected: str) -> None:
        assert collapse_whitespace(raw) == expected


class TestChannelIdentifier:
    """Whatever an admin types must resolve to one chat id form."""

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("@turon_avto", "@turon_avto"),
            ("turon_avto", "@turon_avto"),
            ("https://t.me/turon_avto", "@turon_avto"),
            ("http://t.me/turon_avto", "@turon_avto"),
            ("t.me/turon_avto", "@turon_avto"),
            ("https://telegram.me/turon_avto", "@turon_avto"),
            ("t.me/turon_avto/", "@turon_avto"),
            ("  @turon_avto  ", "@turon_avto"),
            ("-1001234567890", "-1001234567890"),
            ("1234567890", "1234567890"),
        ],
    )
    def test_accepted(self, raw: str, expected: str) -> None:
        assert normalize_channel_identifier(raw) == expected

    @pytest.mark.parametrize("raw", ["", "   ", "@ab", "@1channel", "@bad-name", "@" + "x" * 40])
    def test_rejected(self, raw: str) -> None:
        with pytest.raises(ValueError):
            normalize_channel_identifier(raw)


class TestRetry:
    """The three-attempts-then-give-up policy."""

    async def test_returns_first_success(self) -> None:
        calls = 0

        async def succeed() -> str:
            nonlocal calls
            calls += 1
            return "ok"

        assert await retry_async(succeed, attempts=3, backoff=0.01) == "ok"
        assert calls == 1

    async def test_retries_then_succeeds(self) -> None:
        calls = 0

        async def flaky() -> str:
            nonlocal calls
            calls += 1
            if calls < 3:
                raise ConnectionError("boom")
            return "ok"

        assert await retry_async(flaky, attempts=3, backoff=0.01) == "ok"
        assert calls == 3

    async def test_gives_up_after_the_budget(self) -> None:
        calls = 0

        async def always_fail() -> str:
            nonlocal calls
            calls += 1
            raise ConnectionError("boom")

        with pytest.raises(RetryError) as info:
            await retry_async(always_fail, attempts=3, backoff=0.01, operation="fetch")

        assert calls == 3
        assert info.value.attempts == 3
        assert "fetch" in str(info.value)

    async def test_unlisted_exception_propagates_immediately(self) -> None:
        calls = 0

        async def wrong_type() -> str:
            nonlocal calls
            calls += 1
            raise TypeError("not transient")

        with pytest.raises(TypeError):
            await retry_async(wrong_type, attempts=3, backoff=0.01, retry_on=(ConnectionError,))
        assert calls == 1, "a non-transient error must not be retried"

    async def test_cancellation_is_never_swallowed(self) -> None:
        """Retrying through a shutdown signal would hang the shutdown."""

        async def cancelled() -> str:
            raise asyncio.CancelledError

        with pytest.raises(asyncio.CancelledError):
            await retry_async(cancelled, attempts=3, backoff=0.01)


class TestLocales:
    """Both catalogs must stay in lockstep."""

    @pytest.fixture
    def catalogs(self) -> dict[str, dict[str, str]]:
        def flatten(node: dict, prefix: str = "") -> dict[str, str]:
            flat: dict[str, str] = {}
            for key, value in node.items():
                path = f"{prefix}.{key}" if prefix else key
                if isinstance(value, dict):
                    flat.update(flatten(value, path))
                else:
                    flat[path] = value
            return flat

        base = Path(__file__).resolve().parents[1] / "bot" / "locales"
        return {
            code: flatten(json.loads((base / f"{code}.json").read_text(encoding="utf-8")))
            for code in SUPPORTED_LANGUAGES
        }

    def test_key_sets_match(self, catalogs: dict[str, dict[str, str]]) -> None:
        reference = set(catalogs["uz"])
        for code, catalog in catalogs.items():
            assert set(catalog) == reference, f"{code} has a different key set"

    def test_placeholders_match(self, catalogs: dict[str, dict[str, str]]) -> None:
        """A placeholder present in one language but not another raises at format time."""
        pattern = re.compile(r"\{(\w+)\}")
        for key in catalogs["uz"]:
            variants = {
                code: set(pattern.findall(catalog[key])) for code, catalog in catalogs.items()
            }
            unique = {frozenset(names) for names in variants.values()}
            assert len(unique) == 1, f"placeholder mismatch for {key}: {variants}"

    def test_no_empty_strings(self, catalogs: dict[str, dict[str, str]]) -> None:
        for code, catalog in catalogs.items():
            empty = [key for key, value in catalog.items() if not value.strip()]
            assert not empty, f"{code} has empty translations: {empty}"


class TestTranslator:
    """Lookup behaviour."""

    def test_resolves_a_dotted_key(self) -> None:
        assert t("menu.statistics", "uz") != "menu.statistics"
        assert t("menu.statistics", "ru") != "menu.statistics"

    def test_missing_key_returns_the_key(self) -> None:
        assert t("nope.not.here", "uz") == "nope.not.here"

    def test_interpolates(self) -> None:
        assert "5" in t("scheduler.wrong_count", "uz", expected=5, got=2)

    @pytest.mark.parametrize(
        ("supplied", "expected"),
        [("ru", "ru"), ("ru-RU", "ru"), ("uz-Latn", "uz"), ("en", "uz"), (None, "uz"), ("", "uz")],
    )
    def test_language_normalisation(self, supplied: str | None, expected: str) -> None:
        assert Translator().normalize(supplied) == expected

    def test_falls_back_to_uzbek_for_an_unknown_language(self) -> None:
        assert t("menu.statistics", "de") == t("menu.statistics", "uz")
