"""Reconnection ladder for long-lived venue and feed connections.

The VPS reality this exists for: websockets drop, the internet flaps, the RPC
endpoint rejects a burst, PM2 restarts the process, and the box reboots. None of
those are exceptional — over 24 hours they are certainties — so recovery is the
normal path rather than an error path.

Backoff is exponential with a ceiling and full jitter. The ceiling stops a long
outage from stretching the retry interval past the length of a market, which would
mean waking up already too late for the next one. The jitter matters because
several connections drop together when the network does, and without it they all
retry on the same schedule forever, hitting the venue in synchronised bursts that
look exactly like an attack.

This module holds no connection and reads no clock. It computes delays; the caller
owns the socket and supplies the time.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Final

from arc.errors import ArcError
from arc.logging_setup import log_event

__all__ = ["ReconnectPolicy", "with_reconnect"]

_DEFAULT_INITIAL: Final[float] = 0.5
_DEFAULT_MAX: Final[float] = 30.0


@dataclass(frozen=True, slots=True)
class ReconnectPolicy:
    """Delay schedule for reconnection attempts."""

    initial_seconds: float = _DEFAULT_INITIAL
    max_seconds: float = _DEFAULT_MAX
    multiplier: float = 2.0

    def delay_for(self, attempt: int, jitter: float = 1.0) -> float:
        """Delay before attempt number `attempt` (1-based).

        `jitter` is a caller-supplied fraction in [0, 1]. It is a parameter rather
        than a random() call so the schedule is reproducible in tests; production
        passes a random fraction.
        """
        if attempt < 1:
            raise ValueError(f"attempt must be at least 1, got {attempt}")
        raw = self.initial_seconds * (self.multiplier ** (attempt - 1))
        return min(self.max_seconds, raw) * max(0.0, min(1.0, jitter))


async def with_reconnect[T](
    operation: Callable[[], Awaitable[T]],
    *,
    policy: ReconnectPolicy,
    label: str,
    jitter: Callable[[], float],
    attempts: int = 0,
    logger: logging.Logger | None = None,
) -> T:
    """Run `operation`, reconnecting on failure until it succeeds.

    `attempts` of 0 means retry forever, which is the correct default for a 24/7
    process: giving up on the price feed after N tries leaves a bot that is running,
    shows a dashboard, and is silently not trading.

    CancelledError propagates untouched. Catching it here would make shutdown hang
    on a reconnect loop that refuses to notice it has been cancelled.
    """
    attempt = 0
    while True:
        attempt += 1
        try:
            return await operation()
        except asyncio.CancelledError:
            raise
        except (ArcError, OSError) as exc:
            if attempts and attempt >= attempts:
                raise
            delay = policy.delay_for(attempt, jitter())
            log_event(
                logging.WARNING,
                "Reconnecting",
                f"{label}  attempt {attempt}  in {delay:.1f}s  ({exc})",
                logger=logger,
            )
            await asyncio.sleep(delay)
