"""safe_edit's handling of the message shapes callbacks actually arrive on."""

from __future__ import annotations

from typing import Any

import pytest
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import EditMessageText
from aiogram.types import InlineKeyboardMarkup

from bot.handlers.helpers import safe_edit


class FakeMessage:
    """A message a callback is attached to.

    ``text`` is ``None`` on a media message — Telegram puts the words in
    ``caption`` instead — which is precisely the case that broke.
    """

    def __init__(self, *, text: str | None) -> None:
        self.text = text
        self.caption = None if text else "photo caption"
        self.chat = type("Chat", (), {"id": -100})()
        self.edited: list[dict[str, Any]] = []
        self.answered: list[dict[str, Any]] = []
        self.deleted = False

    async def edit_text(self, **kwargs: Any) -> FakeMessage:
        if self.text is None:
            raise TelegramBadRequest(
                method=EditMessageText(chat_id=-100, message_id=1, text="x"),
                message="Bad Request: there is no text in the message to edit",
            )
        self.edited.append(kwargs)
        return self

    async def answer(self, **kwargs: Any) -> FakeMessage:
        self.answered.append(kwargs)
        return self

    async def delete(self) -> None:
        self.deleted = True


class FakeCallback:
    """Minimal callback wrapper."""

    def __init__(self, message: FakeMessage | None) -> None:
        self.message = message


@pytest.fixture(autouse=True)
def _accept_fakes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Let the isinstance(Message) guard accept the fake."""
    import bot.handlers.helpers as helpers

    monkeypatch.setattr(helpers, "Message", FakeMessage)


class TestSafeEditOnMediaMessages:
    """The admin panel hangs off the greeting, which carries the logo.

    Every panel button therefore arrives on a photo message. Calling edit_text on
    one is refused outright with "there is no text in the message to edit", so
    before this was handled the entire panel was dead on the first click.
    """

    async def test_a_photo_message_is_replaced_not_edited(self) -> None:
        message = FakeMessage(text=None)

        await safe_edit(FakeCallback(message), "Panel text")  # type: ignore[arg-type]

        assert message.edited == [], "edit_text must not be attempted on media"
        assert len(message.answered) == 1, "a replacement message should be sent"
        assert message.answered[0]["text"] == "Panel text"

    async def test_the_photo_message_is_cleaned_up(self) -> None:
        """Leaving it behind would stack a logo above every panel screen."""
        message = FakeMessage(text=None)

        await safe_edit(FakeCallback(message), "Panel text")  # type: ignore[arg-type]

        assert message.deleted is True

    async def test_the_keyboard_survives_the_replacement(self) -> None:
        message = FakeMessage(text=None)
        markup = InlineKeyboardMarkup(inline_keyboard=[])

        await safe_edit(FakeCallback(message), "Panel", markup)  # type: ignore[arg-type]

        assert message.answered[0]["reply_markup"] is markup

    async def test_a_text_message_is_still_edited_in_place(self) -> None:
        """The ordinary path must not turn into delete-and-resend."""
        message = FakeMessage(text="Old panel")

        await safe_edit(FakeCallback(message), "New panel")  # type: ignore[arg-type]

        assert len(message.edited) == 1
        assert message.answered == []
        assert message.deleted is False
