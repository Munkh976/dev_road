"""
IBKR historical-data pacing.

IBKR enforces: no more than 60 historical-data requests in any 10-minute
window. Exceeding it triggers a 10-minute cooldown on the whole client, and
sustained abuse disconnects the API session. The limit applies to all clients
and cannot be worked around.

60 / 10 min = 6 per minute. This module makes that ceiling structural rather
than something a caller has to remember.

Two mechanisms, deliberately both:

  TokenBucket    smooths the request rate to a steady 6/min
  SlidingWindow  hard-blocks if 60 requests have gone out in the last 10 min,
                 which catches bursts the bucket alone would allow after an
                 idle period

The sliding window is the one that actually saves you. A bucket that has been
idle for an hour has a full reserve and will happily fire 60 requests in ten
seconds.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

# IBKR's documented ceiling. Do not raise these.
IBKR_MAX_REQUESTS = 60
IBKR_WINDOW_SECONDS = 600
IBKR_COOLDOWN_SECONDS = 600


@dataclass
class PacingLimiter:
    """Thread-safe rate limiter for IBKR historical data requests.

    Usage:
        limiter = PacingLimiter(requests_per_minute=6)
        for symbol in symbols:
            limiter.acquire()          # blocks until it is safe to request
            bars = ib.reqHistoricalData(...)
    """

    requests_per_minute: float = 6.0
    max_requests: int = IBKR_MAX_REQUESTS
    window_seconds: float = IBKR_WINDOW_SECONDS

    _timestamps: deque[float] = field(default_factory=deque, init=False)
    _tokens: float = field(default=0.0, init=False)
    _last_refill: float = field(default_factory=time.monotonic, init=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False)
    _cooldown_until: float = field(default=0.0, init=False)

    def __post_init__(self) -> None:
        if self.requests_per_minute > 6:
            raise ValueError(
                f"{self.requests_per_minute}/min exceeds IBKR's 60-per-10-minute "
                "limit. The ceiling is 6/min."
            )
        # Start with a partial reserve, not a full one: a full bucket at
        # startup would permit an immediate burst.
        self._tokens = 1.0
        self._capacity = min(5.0, self.requests_per_minute)

    # ---------------------------------------------------------------- public

    def acquire(self, timeout: float | None = None) -> None:
        """Block until one request may be made. Raises TimeoutError if the
        wait would exceed `timeout` seconds."""
        deadline = None if timeout is None else time.monotonic() + timeout

        while True:
            with self._lock:
                wait = self._wait_needed()
                if wait <= 0:
                    self._consume()
                    return

            if deadline is not None and time.monotonic() + wait > deadline:
                raise TimeoutError(
                    f"pacing wait of {wait:.1f}s would exceed timeout {timeout}s"
                )
            # Cap each sleep so a cooldown remains interruptible.
            time.sleep(min(wait, 5.0))

    def enter_cooldown(self, seconds: float = IBKR_COOLDOWN_SECONDS) -> None:
        """Call this when IBKR returns error 420 (pacing violation).

        Nothing gets requested until the cooldown expires. Retrying
        immediately is what turns a cooldown into a disconnect.
        """
        with self._lock:
            self._cooldown_until = time.monotonic() + seconds
            log.warning("IBKR pacing violation — cooling down for %.0fs", seconds)

    @property
    def in_cooldown(self) -> bool:
        with self._lock:
            return time.monotonic() < self._cooldown_until

    def estimate_seconds(self, n_requests: int) -> float:
        """How long `n_requests` will take at this rate. Use it to tell the
        user up front rather than leaving them watching a silent terminal."""
        return n_requests / self.requests_per_minute * 60.0

    def stats(self) -> dict[str, float | int]:
        with self._lock:
            self._evict(time.monotonic())
            return {
                "requests_in_window": len(self._timestamps),
                "window_capacity": self.max_requests,
                "tokens_available": round(self._tokens, 2),
                "cooldown_remaining": max(
                    0.0, round(self._cooldown_until - time.monotonic(), 1)
                ),
            }

    # --------------------------------------------------------------- private

    def _wait_needed(self) -> float:
        now = time.monotonic()

        if now < self._cooldown_until:
            return self._cooldown_until - now

        self._evict(now)

        # Hard sliding-window check — this is the real protection.
        if len(self._timestamps) >= self.max_requests:
            oldest = self._timestamps[0]
            return (oldest + self.window_seconds) - now + 0.1

        # Token bucket smooths the rate.
        self._refill(now)
        if self._tokens < 1.0:
            return (1.0 - self._tokens) / (self.requests_per_minute / 60.0)

        return 0.0

    def _refill(self, now: float) -> None:
        elapsed = now - self._last_refill
        self._tokens = min(
            self._capacity, self._tokens + elapsed * (self.requests_per_minute / 60.0)
        )
        self._last_refill = now

    def _consume(self) -> None:
        self._tokens -= 1.0
        self._timestamps.append(time.monotonic())

    def _evict(self, now: float) -> None:
        cutoff = now - self.window_seconds
        while self._timestamps and self._timestamps[0] < cutoff:
            self._timestamps.popleft()
