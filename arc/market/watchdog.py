"""Feed staleness. Warn, then block trading; recover without a restart.

Two thresholds, both from validated configuration:

    FEED_STALE_WARN_MS       the operator is told; trading continues
    FEED_STALE_CRITICAL_MS   trading is blocked until fresh data arrives

Recovery is automatic and requires no restart. A watchdog that latched would turn
a two-second network hiccup into a dead process for the rest of the session, and
the operator would learn to restart the bot reflexively — which is worse than the
hiccup, because a restart during an open window loses the in-memory market.

TRAP 1, restated here because this is the module most likely to violate it: the
gap between updates says NOTHING about the length of the settlement TWAP window.
30s and 60s are LOOKBACK windows, not publication rates. This module measures the
gap only to answer "is data arriving", and never to infer or check a window length.
Nothing here reads or asserts window_seconds; that assertion lives in
settlement_feed.py where the payload's own declared field can be checked directly.
"""

from __future__ import annotations

from typing import Final

from arc.clock import Clock

__all__ = ["HEALTH_BLOCKED", "HEALTH_OK", "HEALTH_WARN", "FeedHealth", "FeedWatchdog"]

HEALTH_OK: Final[str] = "OK"
HEALTH_WARN: Final[str] = "WARN"
HEALTH_BLOCKED: Final[str] = "BLOCKED"


class FeedHealth:
    """The three states, as a namespace for callers that prefer a dotted name."""

    OK: Final[str] = HEALTH_OK
    WARN: Final[str] = HEALTH_WARN
    BLOCKED: Final[str] = HEALTH_BLOCKED


class FeedWatchdog:
    """Staleness for ONE feed. Instance state; nothing at module scope (A11).

    Uses the injected Clock's MONOTONIC reading, not wall time. An NTP step
    correction — which chrony will apply on this VPS — can move wall time backwards
    by seconds, and a wall-clock watchdog would either report a negative age or
    declare a healthy feed critically stale at the moment the correction landed.
    """

    __slots__ = ("_clock", "_critical_ms", "_last_tick", "_status", "_warn_ms", "transitions")

    def __init__(self, clock: Clock, *, warn_ms: int, critical_ms: int) -> None:
        if warn_ms <= 0:
            raise ValueError(f"warn_ms must be positive, got {warn_ms}")
        if critical_ms <= warn_ms:
            raise ValueError(
                f"critical_ms ({critical_ms}) must exceed warn_ms ({warn_ms}); "
                "otherwise the warning never fires before the block"
            )
        self._clock = clock
        self._warn_ms = warn_ms
        self._critical_ms = critical_ms
        # None, not "now": a watchdog that has never seen a tick has not observed a
        # healthy feed, and starting the age at zero would report OK for a
        # connection that was never established.
        self._last_tick: float | None = None
        self._status = HEALTH_BLOCKED
        self.transitions = 0

    @property
    def warn_ms(self) -> int:
        return self._warn_ms

    @property
    def critical_ms(self) -> int:
        return self._critical_ms

    @property
    def status(self) -> str:
        """Last computed status. Does not advance the clock; call evaluate() for that."""
        return self._status

    @property
    def has_ticked(self) -> bool:
        return self._last_tick is not None

    @property
    def blocked(self) -> bool:
        return self._status == HEALTH_BLOCKED

    def tick(self) -> None:
        """Record that fresh data arrived. Called once per ACCEPTED observation.

        Rejected observations deliberately do not tick. A feed sending a steady
        stream of malformed payloads is not a live feed for trading purposes, and
        ticking on arrival rather than on acceptance would report it healthy.
        """
        self._last_tick = self._clock.monotonic()
        self._set(HEALTH_OK)

    def age_ms(self) -> float | None:
        """Milliseconds since the last accepted observation. None before the first."""
        if self._last_tick is None:
            return None
        return (self._clock.monotonic() - self._last_tick) * 1000.0

    def evaluate(self) -> str:
        """Recompute and return the status. Recovers on its own once data returns.

        Recovery happens through tick(), not here: this is a pure read of elapsed
        time, so calling it repeatedly can only ever hold or worsen the status.
        """
        age = self.age_ms()
        if age is None or age >= self._critical_ms:
            self._set(HEALTH_BLOCKED)
        elif age >= self._warn_ms:
            self._set(HEALTH_WARN)
        else:
            self._set(HEALTH_OK)
        return self._status

    def mark_disconnected(self) -> str:
        """A dropped connection blocks immediately, without waiting for the timer.

        The socket closing is direct evidence that no data is coming; making the
        watchdog wait out critical_ms first would leave a window in which trading
        was permitted against a feed known to be gone.
        """
        self._last_tick = None
        self._set(HEALTH_BLOCKED)
        return self._status

    def _set(self, status: str) -> None:
        if status != self._status:
            self._status = status
            self.transitions += 1
