"""Text helpers and the Bot API length limits we must respect.

Telegram rejects an over-long poll outright, which would silently cost a
scheduled post. Every string that leaves the bot is clamped through here first.
"""

from __future__ import annotations

import html
import re

#: Bot API limits (https://core.telegram.org/bots/api#sendpoll).
POLL_QUESTION_LIMIT = 300
POLL_OPTION_LIMIT = 100
POLL_EXPLANATION_LIMIT = 200
CAPTION_LIMIT = 1024
MESSAGE_LIMIT = 4096

#: Telegram's own ceiling on quiz-poll options. The lower bound of two lives in
#: bot.sources.base; this is the value the database constraint is built from.
MAX_OPTION_COUNT = 10

_WHITESPACE_RE = re.compile(r"\s+")
_ELLIPSIS = "…"


def collapse_whitespace(value: str) -> str:
    """Flatten newlines and runs of spaces into single spaces.

    Poll questions and options are rendered on one line by Telegram, so scraped
    or imported text carrying stray newlines looks broken without this.
    """
    return _WHITESPACE_RE.sub(" ", value).strip()


def truncate(value: str, limit: int, *, ellipsis: str = _ELLIPSIS) -> str:
    """Shorten ``value`` to at most ``limit`` characters.

    Cuts on a word boundary when one is available in the last 20% of the budget,
    so the result reads as a clipped sentence rather than a severed word.

    Args:
        value: Text to shorten.
        limit: Maximum length of the result, including the ellipsis.
        ellipsis: Marker appended when truncation happened.

    Returns:
        ``value`` unchanged when it already fits, otherwise a shortened copy.
    """
    if limit <= 0:
        return ""
    if len(value) <= limit:
        return value
    if limit <= len(ellipsis):
        return value[:limit]

    budget = limit - len(ellipsis)
    head = value[:budget]
    boundary = head.rfind(" ")
    if boundary >= int(budget * 0.8):
        head = head[:boundary]
    return head.rstrip() + ellipsis


def normalize_for_poll(value: str, limit: int) -> str:
    """Prepare arbitrary text for a poll field: collapse, then clamp."""
    return truncate(collapse_whitespace(value), limit)


def escape_html(value: str) -> str:
    """Escape text for Telegram's HTML parse mode."""
    return html.escape(value, quote=False)


def normalize_channel_identifier(value: str) -> str:
    """Normalise admin input into something the Bot API accepts.

    Accepts ``@name``, ``name``, a ``t.me/name`` link, or a raw numeric id
    (including the ``-100…`` supergroup form) and returns either ``@name`` or the
    numeric id as a string.

    Args:
        value: Whatever the admin typed.

    Returns:
        A chat identifier usable as ``chat_id``.

    Raises:
        ValueError: The input could not be interpreted as a channel.
    """
    candidate = value.strip()
    if not candidate:
        raise ValueError("empty channel identifier")

    # Numeric id, e.g. -1001234567890
    if candidate.lstrip("-").isdigit():
        return candidate

    # Strip a t.me / telegram.me link down to its username.
    candidate = re.sub(
        r"^(?:https?://)?(?:www\.)?(?:t|telegram)\.me/", "", candidate, flags=re.IGNORECASE
    )
    candidate = candidate.split("?", 1)[0].strip("/")
    candidate = candidate.lstrip("@")

    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{3,31}", candidate):
        raise ValueError(
            "a channel username must be 5-32 characters, start with a letter, "
            "and contain only letters, digits and underscores"
        )
    return f"@{candidate}"
