from __future__ import annotations

import asyncio
import logging
from typing import TypeVar, Callable, Any

from funpairdl.constants import MAX_RETRIES, RETRY_BASE_DELAY

logger = logging.getLogger("funpairdl.retry")

T = TypeVar("T")


async def retry_async(
    func: Callable[..., Any],
    *args: Any,
    max_retries: int = MAX_RETRIES,
    base_delay: float = RETRY_BASE_DELAY,
    retryable_exceptions: tuple[type[Exception], ...] = (Exception,),
    **kwargs: Any,
) -> Any:
    last_exception = None
    for attempt in range(max_retries + 1):
        try:
            return await func(*args, **kwargs)
        except retryable_exceptions as e:
            last_exception = e
            if attempt < max_retries:
                delay = base_delay * (2 ** attempt)
                logger.warning(
                    "Attempt %d/%d failed: %s. Retrying in %.1fs...",
                    attempt + 1, max_retries + 1, e, delay,
                )
                await asyncio.sleep(delay)
            else:
                logger.error(
                    "All %d attempts failed. Last error: %s",
                    max_retries + 1, e,
                )
    raise last_exception  # type: ignore[misc]
