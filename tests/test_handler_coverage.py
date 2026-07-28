"""Every button in the panel must reach a handler.

A dead button is the worst kind of bug here: it looks fine in review, it looks
fine on the keyboard, and it fails silently in front of the admin with nothing in
the logs. These tests walk the registered routers and check that each callback
the keyboards can emit is actually matched by something.
"""

from __future__ import annotations

import inspect

import pytest
from aiogram import Router
from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardMarkup

from bot.handlers import build_root_router
from bot.keyboards import (
    BATCH_SIZE_OPTIONS,
    CB,
    backup_keyboard,
    batch_size_keyboard,
    channels_keyboard,
    confirm_keyboard,
    hour_keyboard,
    import_keyboard,
    logs_keyboard,
    main_menu_keyboard,
    minute_keyboard,
    posts_per_day_keyboard,
    scheduler_keyboard,
    settings_keyboard,
    stats_keyboard,
)
from bot.sources.registry import WEB_SOURCES

ADMIN_IDS = frozenset({1})


def _every_callback(markup: InlineKeyboardMarkup) -> list[str]:
    """Callback payloads a keyboard can produce."""
    return [
        button.callback_data
        for row in markup.inline_keyboard
        for button in row
        if button.callback_data
    ]


def _all_panel_callbacks() -> set[str]:
    """Union of every callback across every admin keyboard."""
    web = [(key, label) for key, (label, _) in WEB_SOURCES.items()]
    keyboards = [
        main_menu_keyboard("uz", paused=False),
        main_menu_keyboard("uz", paused=True),
        scheduler_keyboard("uz", paused=False),
        scheduler_keyboard("uz", paused=True),
        posts_per_day_keyboard("uz"),
        batch_size_keyboard("uz", 1),
        hour_keyboard("uz", index=0, total=3, picked=[]),
        hour_keyboard("uz", index=1, total=3, picked=["08:00"]),
        minute_keyboard("uz", 8),
        stats_keyboard("uz"),
        import_keyboard("uz", ["a.json"], web),
        backup_keyboard("uz"),
        logs_keyboard("uz"),
        settings_keyboard("uz"),
        channels_keyboard("uz", []),
        confirm_keyboard("uz", CB.SEND_NOW_CONFIRM),
    ]
    found: set[str] = set()
    for markup in keyboards:
        found.update(_every_callback(markup))
    return found


def _callback_matchers(router: Router) -> list[object]:
    """Every callback_query handler across the router tree."""
    handlers: list[object] = []
    stack = [router]
    while stack:
        current = stack.pop()
        handlers.extend(current.callback_query.handlers)
        stack.extend(current.sub_routers)
    return handlers


async def _is_matched(payload: str, handlers: list[object]) -> bool:
    """Whether any handler's filters accept this callback payload."""

    class FakeCallback:
        """Minimal stand-in carrying just the field filters inspect."""

        data = payload
        message = None
        from_user = None

    for handler in handlers:
        for callback_filter in handler.filters or ():  # type: ignore[attr-defined]
            call = callback_filter.callback
            if isinstance(call, type) and issubclass(call, CallbackData):
                continue
            try:
                result = call(FakeCallback())  # type: ignore[arg-type]
                if inspect.isawaitable(result):
                    result = await result
            except Exception:  # noqa: BLE001 - a filter that cannot judge is a miss
                continue
            if result:
                return True
    return False


class TestButtonCoverage:
    """No button may lead nowhere."""

    @pytest.fixture(scope="class")
    def handlers(self) -> list[object]:
        return _callback_matchers(build_root_router(ADMIN_IDS))

    async def test_every_panel_button_has_a_handler(self, handlers: list[object]) -> None:
        orphans = [
            payload
            for payload in sorted(_all_panel_callbacks())
            if not await _is_matched(payload, handlers)
        ]
        assert orphans == [], f"buttons with no handler: {orphans}"

    @pytest.mark.parametrize("count", BATCH_SIZE_OPTIONS)
    async def test_every_batch_size_is_handled(self, count: int, handlers: list[object]) -> None:
        assert await _is_matched(f"{CB.SCHED_BATCH_SET}:{count}", handlers)

    @pytest.mark.parametrize("hour", [0, 7, 13, 23])
    async def test_every_hour_is_handled(self, hour: int, handlers: list[object]) -> None:
        assert await _is_matched(f"{CB.SCHED_HOUR}:{hour}", handlers)

    @pytest.mark.parametrize("minute", [0, 15, 30, 45])
    async def test_every_minute_is_handled(self, minute: int, handlers: list[object]) -> None:
        assert await _is_matched(f"{CB.SCHED_MINUTE}:{minute}", handlers)

    @pytest.mark.parametrize("key", sorted(WEB_SOURCES))
    async def test_every_web_source_button_is_handled(
        self, key: str, handlers: list[object]
    ) -> None:
        assert await _is_matched(f"{CB.IMPORT_WEB}:{key}", handlers)


class TestKeyboardIntegrity:
    """Telegram rejects malformed keyboards outright."""

    def test_no_callback_exceeds_the_64_byte_limit(self) -> None:
        """Telegram silently refuses to render a button over the limit."""
        oversized = [payload for payload in _all_panel_callbacks() if len(payload.encode()) > 64]
        assert oversized == [], f"callback data too long: {oversized}"

    def test_no_button_has_empty_text(self) -> None:
        """An empty label is rejected by the API and kills the whole message."""
        web = [(key, label) for key, (label, _) in WEB_SOURCES.items()]
        for markup in (
            main_menu_keyboard("uz", paused=False),
            scheduler_keyboard("ru", paused=True),
            import_keyboard("ru", ["x.csv"], web),
            hour_keyboard("ru", index=0, total=1, picked=[]),
            minute_keyboard("ru", 12),
        ):
            for row in markup.inline_keyboard:
                for button in row:
                    assert button.text.strip(), "a button label is empty"

    def test_both_languages_render_every_keyboard(self) -> None:
        """A missing translation key would raise while building the panel."""
        web = [(key, label) for key, (label, _) in WEB_SOURCES.items()]
        for language in ("uz", "ru"):
            assert _every_callback(main_menu_keyboard(language, paused=False))
            assert _every_callback(scheduler_keyboard(language, paused=False))
            assert _every_callback(batch_size_keyboard(language, 5))
            assert _every_callback(import_keyboard(language, [], web))
            assert _every_callback(hour_keyboard(language, index=0, total=2, picked=["09:00"]))
