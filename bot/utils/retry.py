"""Retry helper for flaky network work.

The spec is explicit: when a remote source or Telegram is unavailable, try three
times, then log, notify, and keep the bot alive. This module is the single place
that policy lives, so every caller behaves identically.
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable
from typing import TypeVar

from bot.utils.logging import get_logger

logger = get_logger(__name__)

T = TypeVar("T")


class RetryError(RuntimeError):
    """Raised when every attempt failed.

    Attributes:
        attempts: How many calls were made.
        last_error: The exception raised by the final attempt.
    """

    def __init__(self, operation: str, attempts: int, last_error: BaseException) -> None:
        super().__init__(f"{operation} failed after {attempts} attempt(s): {last_error}")
        self.operation = operation
        self.attempts = attempts
        self.last_error = last_error


async def retry_async(
    func: Callable[[], Awaitable[T]],
    *,
    attempts: int = 3,
    backoff: float = 2.0,
    max_backoff: float = 60.0,
    retry_on: tuple[type[BaseException], ...] = (Exception,),
    give_up_on: tuple[type[BaseException], ...] = (asyncio.CancelledError,),
    operation: str = "operation",
) -> T:
    """Call ``func`` until it succeeds or the attempt budget is exhausted.

    Waits ``backoff * 2 ** n`` seconds between attempts with a little jitter, so
    that several jobs failing at the same scheduled minute do not retry in
    lockstep and hammer the remote host.

    Args:
        func: Zero-argument coroutine factory. Passed as a callable rather than a
            coroutine so each attempt gets a fresh awaitable.
        attempts: Maximum number of calls. Must be >= 1.
        backoff: Base delay in seconds.
        max_backoff: Ceiling for a single sleep.
        retry_on: Exceptions considered transient.
        give_up_on: Exceptions that must propagate untouched. Cancellation is
            included by default — retrying through a shutdown signal would hang it.
        operation: Human-readable label used in logs and the final error.

    Returns:
        Whatever ``func`` returned on its first successful call.

    Raises:
        RetryError: Every attempt failed.
    """
    if attempts < 1:
        raise ValueError("attempts must be >= 1")

    last_error: BaseException | None = None

    for attempt in range(1, attempts + 1):
        try:
            return await func()
        except give_up_on:
            raise
        except retry_on as exc:
            last_error = exc
            if attempt == attempts:
                break
            delay = min(backoff * (2 ** (attempt - 1)), max_backoff)
            delay += random.uniform(0, delay * 0.1)  # noqa: S311 - jitter, not crypto
            logger.warning(
                "%s failed (attempt %d/%d): %s — retrying in %.1fs",
                operation,
                attempt,
                attempts,
                exc,
                delay,
                extra={"operation": operation, "attempt": attempt},
            )
            await asyncio.sleep(delay)

    assert last_error is not None  # noqa: S101 - loop always assigns before break
    logger.error(
        "%s failed after %d attempt(s): %s",
        operation,
        attempts,
        last_error,
        extra={"operation": operation, "attempts": attempts},
    )
    raise RetryError(operation, attempts, last_error)
