"""Image download, validation and local caching.

Images are fetched once at import time and served from disk forever after, so a
question can still be published when the original host is unreachable — the same
reasoning that keeps questions in PostgreSQL rather than scraped per send.
"""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlparse

import aiohttp

from bot.utils.logging import get_logger
from bot.utils.retry import RetryError, retry_async

logger = get_logger(__name__)

#: Telegram rejects photos above 10 MB; refusing earlier saves a wasted upload.
MAX_IMAGE_BYTES = 10 * 1024 * 1024

#: Content types we accept, mapped to the extension we store them under.
_CONTENT_TYPE_EXTENSIONS = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "image/bmp": ".bmp",
}

_VALID_EXTENSIONS = frozenset({".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"})

#: Magic-number prefixes, checked because a server's Content-Type is a claim,
#: not a guarantee, and writing a 404 HTML page as "image.jpg" would only fail
#: later at send time.
_MAGIC_SIGNATURES: tuple[tuple[bytes, str], ...] = (
    (b"\xff\xd8\xff", ".jpg"),
    (b"\x89PNG\r\n\x1a\n", ".png"),
    (b"GIF87a", ".gif"),
    (b"GIF89a", ".gif"),
    (b"BM", ".bmp"),
)

_HTTP_TIMEOUT = aiohttp.ClientTimeout(total=60, connect=15)

_USER_AGENT = "TuronAvtoTestBot/1.0 (+https://github.com/m-werzod)"


class MediaError(RuntimeError):
    """An image could not be fetched or stored."""


@dataclass(slots=True)
class MediaResult:
    """Where an image ended up."""

    relative_path: str
    absolute_path: Path
    size_bytes: int
    reused: bool = False


def _detect_extension(payload: bytes, content_type: str | None, url: str) -> str | None:
    """Determine a file extension from content, header, then URL.

    Content first: the bytes are the only source that cannot lie.

    Returns:
        A dotted extension, or ``None`` when the payload is not a known image.
    """
    for signature, extension in _MAGIC_SIGNATURES:
        if payload.startswith(signature):
            return extension

    # WEBP: "RIFF" then 4 size bytes then "WEBP".
    if payload[:4] == b"RIFF" and payload[8:12] == b"WEBP":
        return ".webp"

    if content_type:
        base = content_type.split(";", 1)[0].strip().lower()
        if base in _CONTENT_TYPE_EXTENSIONS:
            return _CONTENT_TYPE_EXTENSIONS[base]

    suffix = Path(unquote(urlparse(url).path)).suffix.lower()
    if suffix in _VALID_EXTENSIONS:
        return suffix

    return None


class MediaService:
    """Downloads and caches question images."""

    def __init__(
        self,
        media_root: Path,
        *,
        max_retries: int = 3,
        retry_backoff: float = 2.0,
        concurrency: int = 8,
    ) -> None:
        """Configure the service.

        Args:
            media_root: Directory images are cached under.
            max_retries: Attempts per download.
            retry_backoff: Base backoff in seconds.
            concurrency: Simultaneous downloads. Kept modest so a bulk import
                does not look like an attack to the origin host.
        """
        self.media_root = media_root
        self.max_retries = max_retries
        self.retry_backoff = retry_backoff
        self._semaphore = asyncio.Semaphore(concurrency)
        self.media_root.mkdir(parents=True, exist_ok=True)

    def _target_path(self, url: str, extension: str) -> tuple[str, Path]:
        """Build a content-addressed destination for a URL.

        Files are named by the hash of their URL and sharded across 256
        subdirectories: a single directory holding 1200+ entries is slow to list
        on some filesystems and unpleasant to inspect by hand.

        Returns:
            The path relative to ``media_root``, and the absolute path.
        """
        digest = hashlib.sha256(url.encode("utf-8")).hexdigest()
        shard = digest[:2]
        relative = f"{shard}/{digest}{extension}"
        return relative, self.media_root / shard / f"{digest}{extension}"

    def find_cached(self, url: str) -> str | None:
        """Return the cached relative path for a URL, if one exists."""
        digest = hashlib.sha256(url.encode("utf-8")).hexdigest()
        shard_dir = self.media_root / digest[:2]
        if not shard_dir.exists():
            return None
        for extension in _VALID_EXTENSIONS:
            candidate = shard_dir / f"{digest}{extension}"
            if candidate.exists() and candidate.stat().st_size > 0:
                return f"{digest[:2]}/{digest}{extension}"
        return None

    def absolute_path(self, relative_path: str) -> Path:
        """Resolve a stored relative path to an absolute one."""
        return self.media_root / relative_path

    async def _fetch_bytes(self, session: aiohttp.ClientSession, url: str) -> tuple[bytes, str | None]:
        """Download a URL, enforcing the size cap while streaming.

        Raises:
            MediaError: Non-200 response, or the body exceeded the cap.
        """
        async with session.get(url, allow_redirects=True) as response:
            if response.status != 200:
                raise MediaError(f"HTTP {response.status} for {url}")

            declared = response.headers.get("Content-Length")
            if declared and declared.isdigit() and int(declared) > MAX_IMAGE_BYTES:
                raise MediaError(
                    f"Image is {int(declared) / 1_048_576:.1f} MB, over the "
                    f"{MAX_IMAGE_BYTES / 1_048_576:.0f} MB limit: {url}"
                )

            # Stream and check as we go: Content-Length is optional and can lie,
            # so the cap must hold without it.
            chunks: list[bytes] = []
            total = 0
            async for chunk in response.content.iter_chunked(64 * 1024):
                total += len(chunk)
                if total > MAX_IMAGE_BYTES:
                    raise MediaError(
                        f"Image exceeds the {MAX_IMAGE_BYTES / 1_048_576:.0f} MB limit: {url}"
                    )
                chunks.append(chunk)

            return b"".join(chunks), response.headers.get("Content-Type")

    async def download(
        self, url: str, session: aiohttp.ClientSession | None = None
    ) -> MediaResult:
        """Fetch an image and cache it locally.

        A previously cached copy is returned immediately, which makes re-running
        an import cheap and makes the whole operation resumable after a failure.

        Args:
            url: Absolute http(s) URL.
            session: Optional shared session. Supplying one across a bulk import
                reuses connections instead of paying a TLS handshake per image.

        Returns:
            Where the image is stored.

        Raises:
            MediaError: The URL is unusable, unreachable, or not an image.
        """
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            raise MediaError(f"Unsupported URL scheme {parsed.scheme!r}: {url}")

        if (cached := self.find_cached(url)) is not None:
            absolute = self.absolute_path(cached)
            return MediaResult(cached, absolute, absolute.stat().st_size, reused=True)

        owns_session = session is None
        if session is None:
            session = aiohttp.ClientSession(
                timeout=_HTTP_TIMEOUT, headers={"User-Agent": _USER_AGENT}
            )

        try:
            async with self._semaphore:
                try:
                    payload, content_type = await retry_async(
                        lambda: self._fetch_bytes(session, url),
                        attempts=self.max_retries,
                        backoff=self.retry_backoff,
                        # A size violation or a 404 will not fix itself; only
                        # retry genuinely transient transport failures.
                        retry_on=(aiohttp.ClientError, TimeoutError, asyncio.TimeoutError),
                        operation=f"download {url}",
                    )
                except RetryError as exc:
                    raise MediaError(str(exc)) from exc
        finally:
            if owns_session:
                await session.close()

        if not payload:
            raise MediaError(f"Empty response body: {url}")

        extension = _detect_extension(payload, content_type, url)
        if extension is None:
            raise MediaError(
                f"Response is not a recognised image (content-type={content_type!r}): {url}"
            )

        relative, absolute = self._target_path(url, extension)
        absolute.parent.mkdir(parents=True, exist_ok=True)

        # Write to a temporary file and rename: an interrupted download must not
        # leave a truncated file that later looks like a valid cache hit.
        temporary = absolute.with_suffix(absolute.suffix + ".part")
        await asyncio.to_thread(temporary.write_bytes, payload)
        await asyncio.to_thread(temporary.replace, absolute)

        logger.info(
            "Downloaded image (%d bytes) -> %s",
            len(payload),
            relative,
            extra={"url": url, "bytes": len(payload)},
        )
        return MediaResult(relative, absolute, len(payload))

    async def download_many(
        self, urls: list[str], *, on_progress: object | None = None
    ) -> dict[str, MediaResult | str]:
        """Download several images over one shared HTTP session.

        Args:
            urls: Image URLs. Duplicates are collapsed.
            on_progress: Optional ``callable(done, total)`` invoked after each
                download. May be sync or async.

        Returns:
            A mapping of URL to :class:`MediaResult` on success, or to the error
            message on failure. Failures never abort the batch — one dead image
            host must not cost an entire import.
        """
        unique = list(dict.fromkeys(urls))
        results: dict[str, MediaResult | str] = {}
        if not unique:
            return results

        completed = 0
        async with aiohttp.ClientSession(
            timeout=_HTTP_TIMEOUT, headers={"User-Agent": _USER_AGENT}
        ) as session:

            async def worker(url: str) -> None:
                nonlocal completed
                try:
                    results[url] = await self.download(url, session=session)
                except MediaError as exc:
                    results[url] = str(exc)
                    logger.warning("Image download failed: %s", exc)
                except Exception as exc:  # noqa: BLE001 - one image must not kill the batch
                    results[url] = f"unexpected error: {exc}"
                    logger.exception("Unexpected error downloading %s", url)
                finally:
                    completed += 1
                    if callable(on_progress):
                        outcome = on_progress(completed, len(unique))
                        if asyncio.iscoroutine(outcome):
                            await outcome

            await asyncio.gather(*(worker(url) for url in unique))

        succeeded = sum(1 for value in results.values() if isinstance(value, MediaResult))
        logger.info("Downloaded %d/%d image(s)", succeeded, len(unique))
        return results

    def storage_stats(self) -> tuple[int, int]:
        """Return the number of cached files and their total size in bytes."""
        count = 0
        total = 0
        for path in self.media_root.rglob("*"):
            if path.is_file() and path.suffix.lower() in _VALID_EXTENSIONS:
                count += 1
                total += path.stat().st_size
        return count, total
