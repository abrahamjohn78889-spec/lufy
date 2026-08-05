"""Outbound token bucket toward the venue (A4).

Cancels BYPASS the bucket entirely. A cancel that waits for a token during the
sweep is a cancel that does not happen before close, and the order it was meant to
retract rides into settlement. Throttling the one operation whose whole purpose is
to reduce exposure gets the trade-off exactly backwards.

Refill is computed from the caller's `now`, never from a clock read here (A10/D1),
so the bucket behaves identically under a frozen clock in tests and under wall time
in production.
"""

from __future__ import annotations

import asyncio
from typing import Final

__all__ = ["TokenBucket"]

_MIN_SLEEP: Final[float] = 0.001


class TokenBucket:
    """Classic token bucket: `burst` capacity, refilled at `sustained` per second."""

    __slots__ = ("_burst", "_last", "_sustained", "_tokens")

    def __init__(self, *, sustained: int, burst: int, now: float) -> None:
        if sustained <= 0:
            raise ValueError(f"sustained rate must be positive, got {sustained}")
        if burst < sustained:
            # Mirrors config rule 13. Held here too because the bucket can also be
            # constructed directly, and a capacity under the refill rate throttles
            # the steady state it was configured to permit.
            raise ValueError(f"burst {burst} must be at least sustained {sustained}")
        self._sustained = float(sustained)
        self._burst = float(burst)
        self._tokens = float(burst)
        self._last = now

    @property
    def tokens(self) -> float:
        return self._tokens

    def _refill(self, now: float) -> None:
        # max(0.0, ...) so a backwards clock step (NTP correction on a VPS) cannot
        # subtract tokens and stall every outbound request until wall time catches up.
        elapsed = max(0.0, now - self._last)
        self._last = now
        self._tokens = min(self._burst, self._tokens + elapsed * self._sustained)

    def take(self, now: float) -> bool:
        """Consume one token if available. Never blocks."""
        self._refill(now)
        if self._tokens < 1.0:
            return False
        self._tokens -= 1.0
        return True

    def delay_until_token(self, now: float) -> float:
        """Seconds until one token exists. Zero when one is available now."""
        self._refill(now)
        if self._tokens >= 1.0:
            return 0.0
        return (1.0 - self._tokens) / self._sustained

    async def acquire(self, now: float) -> None:
        """Wait for one token, then consume it.

        Sleeps the computed deficit in one go rather than polling, and re-checks
        afterwards because another waiter may have taken the token meanwhile.
        """
        while not self.take(now):
            delay = self.delay_until_token(now)
            await asyncio.sleep(max(_MIN_SLEEP, delay))
            now += max(_MIN_SLEEP, delay)
