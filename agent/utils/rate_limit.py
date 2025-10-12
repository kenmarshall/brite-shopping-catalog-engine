import asyncio
import time
from collections import deque


class RateLimiter:
    def __init__(self, rate_per_minute: int) -> None:
        self.rate_per_minute = rate_per_minute
        self._timestamps: deque[float] = deque()

    async def acquire(self) -> None:
        now = time.monotonic()
        window = 60.0
        while self._timestamps and now - self._timestamps[0] > window:
            self._timestamps.popleft()
        if len(self._timestamps) >= self.rate_per_minute:
            sleep_for = window - (now - self._timestamps[0])
            await asyncio.sleep(max(sleep_for, 0))
        self._timestamps.append(time.monotonic())


__all__ = ["RateLimiter"]
