"""Injected clock and drift monitoring.

Nothing in ARC calls time.time() directly. Every component that needs the time
takes a Clock. That is what makes the timing tests deterministic: FrozenClock
lets a test place the process at exactly close_ts - 3.0s and assert what happens,
which is impossible against a wall clock.

Note the boundary this does NOT cross: the execution engine never reads a clock at
all (A10/D1). The clock drives market-grid rotation and window activation. Whether
an order is "too late" is not a question anything in this system asks.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

__all__ = ["Clock", "DriftMonitor", "DriftStatus", "FrozenClock", "SystemClock"]


@runtime_checkable
class Clock(Protocol):
    """Source of wall-clock time, in UTC seconds since the epoch."""

    def now(self) -> float:
        """Current UTC time as a float of seconds since the epoch."""
        ...

    def monotonic(self) -> float:
        """Monotonic seconds, for measuring durations.

        Separate from now() because wall time can step backwards when chrony
        corrects the system clock. A duration measured across an NTP step using
        wall time can come out negative, which would make a staleness check read
        a stalled feed as fresh.
        """
        ...


class SystemClock:
    """The real clock. Used everywhere outside tests."""

    __slots__ = ()

    def now(self) -> float:
        return time.time()

    def monotonic(self) -> float:
        return time.monotonic()


class FrozenClock:
    """A clock the test controls.

    Not a mock of a clock — it is a real Clock implementation whose time only
    changes when something calls advance() or set(). Window activation is
    level-triggered (A12), and the only honest way to prove that a window still
    opens when the event loop was busy through its exact activation instant is to
    jump the clock straight past it.
    """

    __slots__ = ("_monotonic", "_now")

    def __init__(self, now: float, monotonic: float = 0.0) -> None:
        self._now = now
        self._monotonic = monotonic

    def now(self) -> float:
        return self._now

    def monotonic(self) -> float:
        return self._monotonic

    def advance(self, seconds: float) -> None:
        """Move both wall and monotonic time forward by the same amount."""
        self._now += seconds
        self._monotonic += seconds

    def set(self, now: float) -> None:
        """Jump wall time to an absolute value, moving monotonic time with it.

        Monotonic time moves by the same delta and never backwards even when wall
        time jumps back, mirroring what a real NTP correction does to the pair.
        """
        delta = now - self._now
        self._now = now
        if delta > 0:
            self._monotonic += delta


class DriftStatus:
    """Drift severity. Plain string constants — these are log and display text."""

    OK = "OK"
    WARN = "WARN"
    CRITICAL = "CRITICAL"


@dataclass(frozen=True, slots=True)
class DriftReading:
    """One drift measurement."""

    offset_ms: float
    status: str

    @property
    def is_critical(self) -> bool:
        return self.status == DriftStatus.CRITICAL


class DriftMonitor:
    """Compares the local clock against a reference (venue) timestamp.

    Thresholds are applied to the ABSOLUTE offset. A local clock 900ms behind the
    venue is exactly as dangerous as one 900ms ahead: behind, the 3-second window
    is evaluated when 2.1 seconds remain and the order is submitted against a book
    that has already moved; ahead, the window is missed entirely. A signed
    comparison would report the negative case as healthy.
    """

    __slots__ = ("_critical_ms", "_last", "_warn_ms")

    def __init__(self, warn_ms: float, critical_ms: float) -> None:
        if warn_ms <= 0 or critical_ms <= 0:
            raise ValueError("drift thresholds must be positive")
        if warn_ms >= critical_ms:
            raise ValueError("drift warn threshold must be below critical threshold")
        self._warn_ms = warn_ms
        self._critical_ms = critical_ms
        self._last: DriftReading | None = None

    @property
    def warn_ms(self) -> float:
        return self._warn_ms

    @property
    def critical_ms(self) -> float:
        return self._critical_ms

    @property
    def last(self) -> DriftReading | None:
        """The most recent reading, or None if nothing has been measured yet.

        None rather than a zero reading: "we have never checked" and "we checked
        and the clock is perfect" must not display identically.
        """
        return self._last

    def classify(self, offset_ms: float) -> str:
        """Classify a signed offset in milliseconds by its magnitude."""
        magnitude = abs(offset_ms)
        if magnitude >= self._critical_ms:
            return DriftStatus.CRITICAL
        if magnitude >= self._warn_ms:
            return DriftStatus.WARN
        return DriftStatus.OK

    def observe(self, local_ts: float, reference_ts: float) -> DriftReading:
        """Record a measurement. Positive offset means the local clock is ahead."""
        offset_ms = (local_ts - reference_ts) * 1000.0
        reading = DriftReading(offset_ms=offset_ms, status=self.classify(offset_ms))
        self._last = reading
        return reading
