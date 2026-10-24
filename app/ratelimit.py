"""Giới hạn số lượt gọi các API tốn quota Gemini, tính theo IP trong cửa sổ trượt.

Đếm trong RAM là đủ vì ứng dụng chạy một instance, một worker (xem render.yaml).
Khi chạy nhiều instance thì cần chuyển bộ đếm sang Redis.
"""

import time
from collections import defaultdict, deque
from collections.abc import Callable


class RateLimiter:
    def __init__(self, window_s: float = 3600, clock: Callable[[], float] = time.monotonic):
        self.window_s = window_s
        self._clock = clock
        self._hits: dict[tuple[str, str], deque[float]] = defaultdict(deque)

    def hit(self, bucket: str, key: str, limit: int) -> float | None:
        """Ghi một lượt. Trả về None nếu còn lượt, hoặc số giây phải chờ nếu đã hết."""
        now = self._clock()
        hits = self._hits[(bucket, key)]
        while hits and hits[0] <= now - self.window_s:
            hits.popleft()
        if len(hits) >= limit:
            return hits[0] + self.window_s - now
        hits.append(now)
        return None

    def clear(self) -> None:
        self._hits.clear()


limiter = RateLimiter()
