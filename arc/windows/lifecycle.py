"""Window state transitions. Monotonic, terminal-safe, and checked rather than trusted.

    PENDING ─freeze──> FROZEN ─trigger──> FIRED
       │                  │
       └──expire──────────┴──expire────> EXPIRED

The graph is deliberately tiny, and everything about it is one-way. Two properties
matter enough to be enforced here rather than left to the call sites:

FIRED and EXPIRED are TERMINAL. A window that has fired must never fire again — it
authorises at most one intent, ever (A12) — and a window that expired without
crossing must never come back to life because a late observation arrived. Both would
be silent: the second fire looks exactly like the first in every log line.

PENDING is re-enterable from nowhere. A frozen window cannot be un-frozen, so there is
no path by which a window's five locked values can be replaced by a second set
computed from a later TWAP. That is the whole point of freezing them (A4/A12).

WindowState has no CANCELLED or REJECTED member and none is added here. A freeze that
is rejected leaves the window PENDING and untouched — the rejection is an exception
and a log line, not a state — so there is no state in which a window holds a partial
freeze. That is what makes "partial freeze is impossible" a structural claim rather
than a convention.
"""

from __future__ import annotations

import logging
from typing import Final

from arc.domain.enums import WindowState
from arc.domain.models import ExecutionWindow
from arc.errors import ArcError
from arc.logging_setup import log_event

__all__ = [
    "LEGAL_TRANSITIONS",
    "TERMINAL_WINDOW_STATES",
    "WindowTransitionError",
    "assert_can_fire",
    "expire_window",
    "is_terminal",
    "transition",
]


class WindowTransitionError(ArcError):
    """An illegal window state transition was attempted.

    Operational rather than fatal: one malformed window must never take the process
    down, because the other four windows in the market are independent and still
    have valid triggers to evaluate (acceptance criterion 18).
    """


# FIRED and EXPIRED accept nothing. Written as empty tuples rather than omitted keys
# so that a terminal state is a stated fact in this table instead of a KeyError
# somewhere else.
LEGAL_TRANSITIONS: Final[dict[WindowState, tuple[WindowState, ...]]] = {
    WindowState.PENDING: (WindowState.FROZEN, WindowState.EXPIRED),
    WindowState.FROZEN: (WindowState.FIRED, WindowState.EXPIRED),
    WindowState.FIRED: (),
    WindowState.EXPIRED: (),
}

TERMINAL_WINDOW_STATES: Final[frozenset[WindowState]] = frozenset(
    {WindowState.FIRED, WindowState.EXPIRED}
)


def is_terminal(state: WindowState) -> bool:
    return state in TERMINAL_WINDOW_STATES


def transition(
    window: ExecutionWindow,
    target: WindowState,
    *,
    logger: logging.Logger | None = None,
) -> None:
    """Move a window to `target`, or raise and leave it exactly as it was.

    Validates BEFORE assigning. A transition that is going to be refused must not
    have written the new state first, or the refusal becomes a log line about a
    change that already happened.

    A no-op transition (target == current) is refused too, rather than silently
    ignored. Asking a FIRED window to fire again is a caller bug — a double-fire
    attempt — and swallowing it would hide exactly the condition criterion 12 exists
    to prevent.
    """
    current = window.state
    allowed = LEGAL_TRANSITIONS.get(current, ())
    if target not in allowed:
        if is_terminal(current):
            raise WindowTransitionError(
                f"window {window.offset_seconds}s is {current.value} (terminal); "
                f"cannot transition to {target.value} — a terminal window never acts again"
            )
        raise WindowTransitionError(
            f"window {window.offset_seconds}s cannot go {current.value} -> {target.value}; "
            f"legal targets are {[s.value for s in allowed] or 'none'}"
        )
    window.state = target
    log_event(
        logging.DEBUG,
        "Window Transition",
        f"{window.offset_seconds}s  {current.value} -> {target.value}",
        logger=logger,
    )


def assert_can_fire(window: ExecutionWindow) -> None:
    """Refuse to fire a window that must not fire. Raises, never returns False.

    Three independent conditions, all of which have to hold, and none of which the
    evaluator should have to remember:

    The window must be FROZEN. A PENDING window has no trigger at all, so "did the
    trigger pass" has no answer and returning False for it would be a guess.

    It must not be terminal — covered by the FROZEN requirement, but stated
    separately because the failure it prevents is the one that matters most: a
    second intent from a window that already produced one.

    Its direction and trigger must both be present. A frozen window always has both
    (freeze is atomic), so this is a structural assertion; if it ever fails, the
    atomicity guarantee has been broken somewhere and firing on a half-frozen window
    would submit an order against a trigger nobody computed.
    """
    if is_terminal(window.state):
        raise WindowTransitionError(
            f"window {window.offset_seconds}s already reached {window.state.value}; "
            "each window may fire at most once (A12)"
        )
    if window.state is not WindowState.FROZEN:
        raise WindowTransitionError(
            f"window {window.offset_seconds}s is {window.state.value}, not FROZEN; "
            "an unfrozen window has no trigger to satisfy"
        )
    if window.direction is None or window.locked_trigger is None:
        raise WindowTransitionError(
            f"window {window.offset_seconds}s is FROZEN but missing "
            f"direction={window.direction} trigger={window.locked_trigger}; "
            "freeze atomicity has been violated"
        )


def expire_window(
    window: ExecutionWindow,
    *,
    logger: logging.Logger | None = None,
) -> bool:
    """End a window that never crossed. Returns False if it was already terminal.

    NOT an error, and deliberately not logged as one (acceptance criterion 8). A
    window whose trigger never came true is the strategy working: the buffer said
    the move was not big enough, so no trade. Logging it at WARNING would train the
    operator to ignore warnings, which is worse than silence.

    Returns a bool instead of raising on a terminal window because expiry is driven
    by market close sweeping every window at once, and a market that had two windows
    fire must not blow up while expiring the other three.
    """
    if is_terminal(window.state):
        return False
    transition(window, WindowState.EXPIRED, logger=logger)
    log_event(
        logging.INFO,
        "Window No Signal",
        f"{window.offset_seconds}s  trigger never crossed",
        logger=logger,
    )
    return True
