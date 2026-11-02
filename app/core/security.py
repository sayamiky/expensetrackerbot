"""In-process rate limiting for the Telegram webhook."""

from collections import defaultdict, deque
from threading import Lock

from fastapi import Depends

from app.core.config import Settings, get_settings


class SlidingWindowRateLimiter:
    """In-process sliding-window limiter, keyed by caller.

    Deliberately simple: state lives in this process only, so it throttles a
    single worker. Put a shared limiter in front (Redis, API gateway) before
    running multiple workers.
    """

    def __init__(self, limit: int, window_seconds: float) -> None:
        self._limit = limit
        self._window = window_seconds
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = Lock()

    def check(self, key: str, now: float) -> float | None:
        """Record a hit. Returns seconds to wait if the caller is over quota."""
        with self._lock:
            hits = self._hits[key]
            cutoff = now - self._window
            while hits and hits[0] <= cutoff:
                hits.popleft()

            if len(hits) >= self._limit:
                return max(0.0, hits[0] + self._window - now)

            hits.append(now)
            return None


_limiter: SlidingWindowRateLimiter | None = None
_limiter_lock = Lock()


def get_rate_limiter(
    settings: Settings = Depends(get_settings),
) -> SlidingWindowRateLimiter:
    """Return the process-wide limiter, built once from settings."""
    global _limiter
    with _limiter_lock:
        if _limiter is None:
            _limiter = SlidingWindowRateLimiter(
                limit=settings.rate_limit_requests,
                window_seconds=settings.rate_limit_window_seconds,
            )
    return _limiter


def reset_rate_limiter() -> None:
    """Drop the shared limiter so tests start from a clean window."""
    global _limiter
    with _limiter_lock:
        _limiter = None
