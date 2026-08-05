"""Window activation. A LEVEL check on every pass — never a scheduled timer.

`due_windows(market, now)` answers one question: which of this market's windows have
passed their activation instant and are still PENDING? It is a pure comparison against
the clock it is handed. It schedules nothing, sleeps nothing, and creates no task.

WHY THIS IS NOT A TIMER (A12, acceptance criteria 1-2)

A timer that fires late is indistinguishable from a timer that never fired. The event
loop is busy processing a burst of feed frames at exactly the moment a 3-second window
comes due; `call_later` fires when the loop next yields, which may be after close_ts,
and the window is simply gone. Nothing logs it, because from the timer's point of view
it did fire — just too late to matter. Worse, a process suspended across the activation
instant (a VPS hiccup, a long fsync) loses the callback entirely.

A level check converges from any starting point. A pass that arrives 200 ms late still
sees `activation_ts <= now` and opens the window on that pass. A pass that arrives 200
passes late opens every window that came due in between, in priority order, in one
sweep. There is no state to reconstruct because there was no event to miss.

The cost is that a window opens on the first pass after its instant rather than exactly
at it, so activation is late by at most one loop interval. That is a bounded, observable
error; a dropped window is neither.

Activation is IDEMPOTENT (criterion 13). `due_windows` filters on PENDING, so a window
that has already been frozen is not returned again no matter how many times the loop
passes over it. Nothing here mutates the window — freezing is freeze.py's job — which
is what keeps "called twice" and "called once" the same thing.
"""

from __future__ import annotations

from arc.domain.enums import MarketPhase, WindowState
from arc.domain.models import ExecutionWindow, MarketInstance
from arc.domain.timing import activation_ts

__all__ = [
    "ACTIVATABLE_PHASES",
    "due_windows",
    "is_activatable",
    "next_activation_ts",
    "window_is_due",
]

# Phases in which a window may open at all.
#
# ACTIVE only. CANCELLING is the sweep — the single execution boundary that exists
# (A10/D1) — and opening a window after it would freeze a trigger no order could ever
# act on. DEAD has no official PTB, so freezing is impossible by construction and a
# window that opened would sit PENDING forever looking like a stall. SETTLING and
# SETTLED are past close_ts, where every window's activation instant is behind it and
# every one of them would come due at once.
ACTIVATABLE_PHASES: frozenset[MarketPhase] = frozenset({MarketPhase.ACTIVE})


def is_activatable(market: MarketInstance) -> bool:
    """Whether this market may open windows at all. Phase gate, not a clock gate."""
    return market.phase in ACTIVATABLE_PHASES


def window_is_due(window: ExecutionWindow, close_ts: int, now: float) -> bool:
    """Has this window's activation instant passed while it is still PENDING?

    The comparison is `>=`, not `>`: a pass landing exactly on the activation instant
    opens the window on that pass. With `>` a window whose instant coincided precisely
    with a loop pass would wait a whole extra interval, which is a real effect at the
    3-second offset.

    Deliberately NOT bounded above by close_ts. A window that came due at close-3s and
    was not reached until close+0.2s is still opened, because refusing it would silently
    drop the best-informed window in the market whenever a pass ran slightly long. The
    phase gate is what stops a genuinely closed market from activating anything, and
    phase is authoritative for that (A10/D1).
    """
    if window.state is not WindowState.PENDING:
        return False
    return now >= activation_ts(close_ts, window.offset_seconds)


def due_windows(market: MarketInstance, now: float) -> tuple[ExecutionWindow, ...]:
    """Every PENDING window whose instant has passed, in priority order: 3, 5, 7, 10, 15.

    Returns a tuple rather than yielding: the caller freezes each of these, and a lazy
    generator would be iterating the same window dict the freeze is mutating.

    Priority order comes from windows_by_priority (ascending offset), so the window
    closest to close is returned first. It has the largest determined fraction behind
    it and is therefore the best-informed (A7), which is why it is tried first (A12).
    """
    if not is_activatable(market):
        return ()
    return tuple(
        window
        for window in market.windows_by_priority()
        if window_is_due(window, market.close_ts, now)
    )


def next_activation_ts(market: MarketInstance) -> int | None:
    """The earliest activation instant still ahead of the windows that are PENDING.

    For DISPLAY and diagnostics only. Nothing in the activation path consumes this —
    if it did, that would be a schedule, and a schedule is what this module exists to
    avoid. Returns None when no window remains PENDING.
    """
    pending = [w for w in market.windows_by_priority() if w.state is WindowState.PENDING]
    if not pending:
        return None
    # Largest offset = earliest instant. The 15s window activates before the 3s one.
    return activation_ts(market.close_ts, max(w.offset_seconds for w in pending))
