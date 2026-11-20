"""Giới hạn tốc độ (token bucket) và thử lại có backoff lũy thừa + jitter."""

from __future__ import annotations

import asyncio
import logging
import secrets
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TypeVar

from vietpdf.translate.base import RetryableError

log = logging.getLogger(__name__)
T = TypeVar("T")
Sleep = Callable[[float], Awaitable[None]]


class TokenBucket:
    """`rpm` request mỗi phút, cho phép dồn tối đa `burst` request."""

    def __init__(
        self, rpm: float, burst: int = 1, *, clock: Callable[[], float] = time.monotonic, sleep: Sleep = asyncio.sleep
    ) -> None:
        if rpm <= 0:
            raise ValueError("rpm phải > 0")
        self.rate = rpm / 60.0
        self.capacity = float(max(1, burst))
        self.tokens = self.capacity
        self._clock = clock
        self._sleep = sleep
        self._last = clock()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            while True:
                now = self._clock()
                self.tokens = min(self.capacity, self.tokens + (now - self._last) * self.rate)
                self._last = now
                if self.tokens >= 1:
                    self.tokens -= 1
                    return
                await self._sleep((1 - self.tokens) / self.rate)


@dataclass
class RetryPolicy:
    max_attempts: int = 6
    base_delay: float = 2.0
    max_delay: float = 60.0

    def delay(self, attempt: int, retry_after: float | None) -> float:
        if retry_after is not None and retry_after >= 0:
            return min(retry_after, self.max_delay)
        d = min(self.base_delay * (2 ** (attempt - 1)), self.max_delay)
        jitter = secrets.SystemRandom().uniform(0, d * 0.25)
        return float(min(d + jitter, self.max_delay))


async def with_retry(
    fn: Callable[[], Awaitable[T]], policy: RetryPolicy, limiter: TokenBucket | None, *, sleep: Sleep = asyncio.sleep
) -> T:
    """Lỗi `RetryableError` được thử lại; mọi lỗi khác (vd. AuthenticationError) ném ra ngay."""
    attempt = 0
    while True:
        attempt += 1
        if limiter is not None:
            await limiter.acquire()
        try:
            return await fn()
        except RetryableError as e:
            if attempt >= policy.max_attempts:
                raise
            d = policy.delay(attempt, e.retry_after)
            log.warning("Lỗi tạm thời (%s), thử lại sau %.1fs (lần %d/%d)", e, d, attempt, policy.max_attempts)
            await sleep(d)


def parse_retry_after(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        return None
