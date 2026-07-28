"""Scraping the official Uzbek driving-test banks.

``e-avtomaktab.uz`` renders its exam page server-side and, alongside the visible
markup, embeds the whole question set as a JavaScript literal::

    const questions = [{"QuestionId": 697, "TextLat": "...", "Image": "...",
                        "Answers": [{"TextLat": "...", "IsCorrect": true}, ...]}]

Reading that literal is dramatically better than parsing the DOM. It carries the
question in four languages, the image URL, and — crucially — ``IsCorrect``, which
appears nowhere in the rendered HTML. Scraping the visible page would yield
questions with no correct answer and therefore no quiz poll. It also means no
browser engine is needed: one HTTP request, no Playwright, no Chromium download.

The page serves a random subset per request, so the scraper polls repeatedly and
deduplicates by ``QuestionId`` until fresh requests stop yielding anything new.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import AsyncIterator
from typing import Any

import aiohttp

from bot.sources.base import (
    MAX_OPTIONS,
    MIN_OPTIONS,
    QuestionSource,
    QuestionValidationError,
    RawQuestion,
    SourceError,
    parse_record,
)
from bot.utils.logging import get_logger

logger = get_logger(__name__)

#: The exam page. Each GET returns a fresh random selection of questions.
E_AVTOMAKTAB_URL = "https://e-avtomaktab.uz/Home/Test"

#: Locates the embedded literal. Deliberately anchored on the declaration rather
#: than on a bare ``[{`` so an unrelated array cannot be picked up by accident.
_QUESTIONS_DECLARATION = re.compile(r"(?:var|let|const)\s+questions\s*=\s*\[", re.IGNORECASE)

#: Site language code to the field holding that language's text.
_LANGUAGE_FIELDS = {
    "uz": "TextLat",
    "ru": "TextRu",
}

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


def _extract_json_array(html: str) -> list[dict[str, Any]]:
    """Pull the ``questions`` array out of a page.

    A regex cannot match balanced brackets, so the opening bracket is located by
    pattern and the matching close found by counting depth — while ignoring
    brackets inside string literals, which appear in real question text.

    Args:
        html: Full page source.

    Returns:
        The decoded array.

    Raises:
        SourceError: The literal is absent or will not decode, which is how a
            site redesign surfaces — as one clear error rather than as a slow
            trickle of malformed questions.
    """
    match = _QUESTIONS_DECLARATION.search(html)
    if match is None:
        raise SourceError(
            "e-avtomaktab.uz did not include the expected 'questions' data. "
            "The site layout has probably changed."
        )

    start = html.index("[", match.end() - 1)
    depth = 0
    in_string = False
    escaped = False

    for index in range(start, len(html)):
        char = html[index]

        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue

        if char == '"':
            in_string = True
        elif char == "[":
            depth += 1
        elif char == "]":
            depth -= 1
            if depth == 0:
                try:
                    payload = json.loads(html[start : index + 1])
                except json.JSONDecodeError as exc:
                    raise SourceError(f"Could not decode the question data: {exc}") from exc
                if not isinstance(payload, list):
                    raise SourceError("Question data was not a list")
                return payload

    raise SourceError("Question data was truncated: no matching closing bracket")


class EAvtomaktabSource(QuestionSource):
    """Questions scraped from e-avtomaktab.uz."""

    def __init__(
        self,
        *,
        language: str = "uz",
        max_requests: int = 120,
        stop_after_barren_rounds: int = 12,
        request_delay: float = 0.7,
        timeout: float = 30.0,
        strict: bool = False,
    ) -> None:
        """Configure the scraper.

        Args:
            language: ``uz`` or ``ru``. Selects which of the four translations
                the stored question uses.
            max_requests: Hard ceiling on HTTP requests, so a site that always
                returns something new cannot loop forever.
            stop_after_barren_rounds: Give up once this many consecutive requests
                add no unseen question. The page samples randomly, so a few
                repeats in a row is normal and a single barren round proves
                nothing.
            request_delay: Seconds between requests. Deliberate politeness — this
                is somebody else's public site.
            timeout: Per-request timeout in seconds.
            strict: Abort on the first unusable record instead of skipping it.
        """
        if language not in _LANGUAGE_FIELDS:
            raise SourceError(
                f"Unsupported language {language!r}; expected one of {sorted(_LANGUAGE_FIELDS)}"
            )

        self.language = language
        self.max_requests = max_requests
        self.stop_after_barren_rounds = stop_after_barren_rounds
        self.request_delay = request_delay
        self.timeout = timeout
        self.strict = strict
        self.name = f"e-avtomaktab-{language}"

    async def count_estimate(self) -> int | None:
        """Unknown ahead of time — the page never states a total."""
        return None

    def _to_record(self, entry: dict[str, Any]) -> dict[str, Any] | None:
        """Convert one site entry into a record ``parse_record`` understands.

        Returns:
            The record, or ``None`` when the entry cannot make a usable quiz.
        """
        field = _LANGUAGE_FIELDS[self.language]
        text = str(entry.get(field) or "").strip()
        if not text:
            return None

        options: list[str] = []
        correct_index: int | None = None
        for answer in entry.get("Answers") or []:
            option = str(answer.get(field) or "").strip()
            if not option:
                continue
            if answer.get("IsCorrect"):
                # First correct option wins; the bank occasionally flags more
                # than one, and a quiz poll accepts exactly one.
                correct_index = correct_index if correct_index is not None else len(options)
            options.append(option)

        if correct_index is None or not MIN_OPTIONS <= len(options) <= MAX_OPTIONS:
            return None

        image = str(entry.get("Image") or "").strip()

        return {
            "external_id": str(entry.get("QuestionId") or "").strip(),
            "text": text,
            "options": options,
            "correct_index": correct_index,
            "image_url": image or None,
            "original_url": E_AVTOMAKTAB_URL,
            "language": self.language,
        }

    async def _fetch_page(self, session: aiohttp.ClientSession) -> list[dict[str, Any]]:
        """Fetch one page and return its question entries."""
        async with session.get(E_AVTOMAKTAB_URL) as response:
            response.raise_for_status()
            html = await response.text()
        return _extract_json_array(html)

    async def fetch(self) -> AsyncIterator[RawQuestion]:  # type: ignore[override]
        """Yield every distinct question the site will hand over.

        Raises:
            SourceError: The site is unreachable or its data cannot be read.
        """
        seen: set[str] = set()
        barren_rounds = 0
        requests_made = 0
        skipped = 0

        timeout = aiohttp.ClientTimeout(total=self.timeout)
        headers = {"User-Agent": _USER_AGENT, "Accept-Language": "uz,ru;q=0.9"}

        async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
            while (
                requests_made < self.max_requests and barren_rounds < self.stop_after_barren_rounds
            ):
                if requests_made:
                    await asyncio.sleep(self.request_delay)

                try:
                    entries = await self._fetch_page(session)
                except aiohttp.ClientError as exc:
                    if not seen:
                        raise SourceError(f"Could not reach e-avtomaktab.uz: {exc}") from exc
                    # Mid-scrape failures are tolerated: keeping several hundred
                    # questions beats discarding them over one dropped request.
                    logger.warning("Request failed mid-scrape (%s); stopping early", exc)
                    break

                requests_made += 1
                fresh = 0

                for entry in entries:
                    identifier = str(entry.get("QuestionId") or "").strip()
                    if not identifier or identifier in seen:
                        continue
                    seen.add(identifier)
                    fresh += 1

                    record = self._to_record(entry)
                    if record is None:
                        skipped += 1
                        continue

                    try:
                        yield parse_record(
                            record,
                            location=f"e-avtomaktab #{identifier}",
                            default_language=self.language,
                        )
                    except QuestionValidationError as exc:
                        if self.strict:
                            raise
                        skipped += 1
                        logger.debug("Skipped question %s: %s", identifier, exc)

                barren_rounds = barren_rounds + 1 if fresh == 0 else 0

        logger.info(
            "e-avtomaktab: %d unique question(s) over %d request(s), %d skipped",
            len(seen),
            requests_made,
            skipped,
        )


# --- avtotestu.uz -------------------------------------------------------------
#: Base for the static ticket files. The site is a single-page app that fetches
#: ``/data/variants/v{n}.json`` for each of its published tickets, so those files
#: *are* the question bank — no browser, no rendering, no scraping heuristics.
AVTOTESTU_BASE = "https://www.avtotestu.uz"
AVTOTESTU_VARIANT_URL = AVTOTESTU_BASE + "/data/variants/v{number}.json"
AVTOTESTU_IMAGE_BASE = AVTOTESTU_BASE + "/images/"

#: Highest ticket published. Requesting beyond it returns the app's index.html
#: with a 200, so the walk stops on content rather than on status code.
AVTOTESTU_MAX_VARIANT = 63

#: Bot language to the key used inside ``content`` and ``izoh``.
_AVTOTESTU_LANGUAGE_KEYS = {
    "uz": "uz_lat",
    "ru": "ru",
}


class AvtotestuSource(QuestionSource):
    """Questions from avtotestu.uz's published ticket files.

    Each ticket is a JSON array of question objects carrying text and options in
    three languages, an ``is_correct`` flag, a media filename and — uniquely
    among these sources — a written explanation, which becomes the quiz poll's
    explanation text.
    """

    def __init__(
        self,
        *,
        language: str = "uz",
        max_variant: int = AVTOTESTU_MAX_VARIANT,
        request_delay: float = 0.3,
        timeout: float = 30.0,
        strict: bool = False,
    ) -> None:
        """Configure the reader.

        Args:
            language: ``uz`` or ``ru``.
            max_variant: Highest ticket number to try.
            request_delay: Seconds between ticket requests.
            timeout: Per-request timeout in seconds.
            strict: Abort on the first unusable record instead of skipping it.
        """
        if language not in _AVTOTESTU_LANGUAGE_KEYS:
            raise SourceError(
                f"Unsupported language {language!r}; "
                f"expected one of {sorted(_AVTOTESTU_LANGUAGE_KEYS)}"
            )

        self.language = language
        self.max_variant = max_variant
        self.request_delay = request_delay
        self.timeout = timeout
        self.strict = strict
        self.name = f"avtotestu-{language}"

    async def count_estimate(self) -> int | None:
        """Twenty questions per ticket, give or take a short final one."""
        return self.max_variant * 20

    def _to_record(self, entry: dict[str, Any]) -> dict[str, Any] | None:
        """Convert one ticket entry into a record ``parse_record`` understands.

        Returns:
            The record, or ``None`` when the entry cannot make a usable quiz.
        """
        key = _AVTOTESTU_LANGUAGE_KEYS[self.language]
        content = (entry.get("content") or {}).get(key) or {}

        text = str(content.get("text") or "").strip()
        if not text:
            return None

        options: list[str] = []
        correct_index: int | None = None
        for option in content.get("options") or []:
            label = str(option.get("text") or "").strip()
            if not label:
                continue
            if option.get("is_correct") and correct_index is None:
                correct_index = len(options)
            options.append(label)

        if correct_index is None or not MIN_OPTIONS <= len(options) <= MAX_OPTIONS:
            return None

        info = entry.get("task_info") or {}
        identifier = str(info.get("global_id") or "").strip()
        if not identifier:
            return None

        media = str(entry.get("media_url") or "").strip()
        image_url = AVTOTESTU_IMAGE_BASE + media.lstrip("/") if media else None

        explanation = None
        izoh = entry.get("izoh")
        if isinstance(izoh, dict):
            explanation = str(izoh.get(key) or "").strip() or None
        elif isinstance(izoh, str):
            explanation = izoh.strip() or None

        ticket = info.get("ticket_num")
        original = f"{AVTOTESTU_BASE}/savol/variant-{ticket}" if ticket else AVTOTESTU_BASE

        return {
            "external_id": identifier,
            "text": text,
            "options": options,
            "correct_index": correct_index,
            "explanation": explanation,
            "image_url": image_url,
            "original_url": original,
            "language": self.language,
            "category": f"Bilet {ticket}" if ticket else None,
        }

    async def _fetch_variant(
        self, session: aiohttp.ClientSession, number: int
    ) -> list[dict[str, Any]] | None:
        """Fetch one ticket file.

        Returns:
            Its entries, or ``None`` when that ticket does not exist. A missing
            ticket answers 200 with the app's HTML shell rather than a 404, so
            the body is what decides.
        """
        url = AVTOTESTU_VARIANT_URL.format(number=number)
        async with session.get(url) as response:
            if response.status != 200:
                return None
            body = await response.text()

        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            return None

        return payload if isinstance(payload, list) else None

    async def fetch(self) -> AsyncIterator[RawQuestion]:  # type: ignore[override]
        """Yield every question across every published ticket.

        Raises:
            SourceError: The first ticket could not be read, which means the site
                or its layout has changed rather than that one file is missing.
        """
        seen: set[str] = set()
        skipped = 0
        tickets = 0

        timeout = aiohttp.ClientTimeout(total=self.timeout)
        headers = {"User-Agent": _USER_AGENT, "Accept": "application/json"}

        async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
            for number in range(1, self.max_variant + 1):
                if number > 1:
                    await asyncio.sleep(self.request_delay)

                try:
                    entries = await self._fetch_variant(session, number)
                except aiohttp.ClientError as exc:
                    if number == 1:
                        raise SourceError(f"Could not reach avtotestu.uz: {exc}") from exc
                    logger.warning("Ticket %d failed (%s); continuing", number, exc)
                    continue

                if entries is None:
                    if number == 1:
                        raise SourceError(
                            "avtotestu.uz did not return ticket data. "
                            "The site layout has probably changed."
                        )
                    logger.debug("Ticket %d is not published; skipping", number)
                    continue

                tickets += 1

                for entry in entries:
                    record = self._to_record(entry)
                    if record is None:
                        skipped += 1
                        continue

                    identifier = str(record["external_id"])
                    if identifier in seen:
                        continue
                    seen.add(identifier)

                    try:
                        yield parse_record(
                            record,
                            location=f"avtotestu {identifier}",
                            default_language=self.language,
                        )
                    except QuestionValidationError as exc:
                        if self.strict:
                            raise
                        skipped += 1
                        logger.debug("Skipped %s: %s", identifier, exc)

        logger.info(
            "avtotestu: %d question(s) across %d ticket(s), %d skipped",
            len(seen),
            tickets,
            skipped,
        )
