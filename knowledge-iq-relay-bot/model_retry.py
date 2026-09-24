"""Retry transient model rate-limit failures with bounded backoff."""

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from typing import TypeVar

logger = logging.getLogger(__name__)

_Result = TypeVar("_Result")
_RATE_LIMIT_MARKERS = ("rate_limit_exceeded", "rate limit exceeded", "error code: 429")


def is_rate_limit_error(exc: BaseException) -> bool:
    """Return whether an exception or one of its nested arguments represents HTTP 429."""
    pending: list[object] = [exc]
    visited: set[int] = set()

    while pending:
        current = pending.pop()
        if id(current) in visited:
            continue
        visited.add(id(current))

        if getattr(current, "status_code", None) == 429:
            return True
        response = getattr(current, "response", None)
        if getattr(response, "status_code", None) == 429:
            return True
        if any(marker in str(current).lower() for marker in _RATE_LIMIT_MARKERS):
            return True

        if isinstance(current, BaseException):
            pending.extend(current.args)
            if current.__cause__ is not None:
                pending.append(current.__cause__)
            if current.__context__ is not None:
                pending.append(current.__context__)

    return False


async def run_with_rate_limit_retry(
    operation: Callable[[], Awaitable[_Result]],
    *,
    max_attempts: int = 3,
    base_delay_seconds: float = 2.0,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> _Result:
    """Run an async model operation and retry transient rate limits."""
    if max_attempts < 1:
        raise ValueError("max_attempts must be at least 1")

    for attempt in range(max_attempts):
        try:
            return await operation()
        except Exception as exc:
            if not is_rate_limit_error(exc) or attempt == max_attempts - 1:
                raise
            delay = base_delay_seconds * (2**attempt)
            logger.warning(
                "Model rate limit reached; retrying in %.1f seconds (attempt %s/%s)",
                delay,
                attempt + 2,
                max_attempts,
            )
            await sleep(delay)

    raise RuntimeError("Model retry loop exited unexpectedly")


def run_with_rate_limit_retry_sync(
    operation: Callable[[], _Result],
    *,
    max_attempts: int = 5,
    base_delay_seconds: float = 2.0,
    sleep: Callable[[float], None] = time.sleep,
) -> _Result:
    """Run a synchronous model operation and retry transient rate limits."""
    if max_attempts < 1:
        raise ValueError("max_attempts must be at least 1")

    for attempt in range(max_attempts):
        try:
            return operation()
        except Exception as exc:
            if not is_rate_limit_error(exc) or attempt == max_attempts - 1:
                raise
            delay = base_delay_seconds * (2**attempt)
            logger.warning(
                "Model rate limit reached; retrying in %.1f seconds (attempt %s/%s)",
                delay,
                attempt + 2,
                max_attempts,
            )
            sleep(delay)

    raise RuntimeError("Model retry loop exited unexpectedly")