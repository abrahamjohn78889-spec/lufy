"""Market-grid timing.

The grid is fixed and contiguous (A5):

    window_ts = floor(now / 300) * 300
    close_ts  = window_ts + 300
    slug      = "btc-updown-5m-{window_ts}"

and the next market's window_ts IS this market's close_ts, which is why two market
instances are alive at once around a boundary and never three (A10/D6).

Everything here is a pure function of a timestamp. Nothing in this module reads a
clock — the caller passes the time in. That is what makes level-triggered window
activation testable: `is_window_open` answers "is the level satisfied at time t",
not "has a timer fired".
"""

from __future__ import annotations

from decimal import Decimal
from typing import Final

from arc.domain.money import to_decimal

__all__ = [
    "MARKET_DURATION_SECONDS",
    "SETTLEMENT_WINDOW_SECONDS",
    "SLUG_PREFIX",
    "activation_ts",
    "cancel_ts",
    "close_ts_for",
    "format_countdown",
    "is_window_open",
    "next_window_ts",
    "settlement_determined_fraction",
    "settlement_window_start",
    "slug_for",
    "window_ts_for",
    "windows_by_priority",
]

MARKET_DURATION_SECONDS: Final[int] = 300
SLUG_PREFIX: Final[str] = "btc-updown-5m-"

# The venue's Chainlink averaging window for 5-minute markets.
#
# TRAP 1 (A5): this is a LOOKBACK LENGTH, not a publication rate. It says the venue
# averages the last 30 seconds of observations; it says nothing about how often the
# feed emits. Never infer one from the other and never health-check one with the
# other — they are unrelated quantities.
SETTLEMENT_WINDOW_SECONDS: Final[int] = 30


def window_ts_for(ts: float) -> int:
    """Grid start of the market containing `ts`."""
    return int(ts // MARKET_DURATION_SECONDS) * MARKET_DURATION_SECONDS


def close_ts_for(window_ts: int) -> int:
    """Close time of the market starting at `window_ts`."""
    return window_ts + MARKET_DURATION_SECONDS


def next_window_ts(window_ts: int) -> int:
    """Start of the following market.

    Identical to close_ts_for by construction: the grid is contiguous, with no gap
    between one market closing and the next opening.
    """
    return window_ts + MARKET_DURATION_SECONDS


def slug_for(window_ts: int) -> str:
    """Polymarket slug for a grid timestamp."""
    return f"{SLUG_PREFIX}{window_ts}"


def activation_ts(close_ts: int, offset_seconds: int) -> int:
    """Instant at which an execution window becomes eligible to open."""
    return close_ts - offset_seconds


def is_window_open(now: float, close_ts: int, offset_seconds: int) -> bool:
    """LEVEL check: has the window's activation instant passed, and is it pre-close?

    Level-triggered, deliberately (A12). A scheduled timer that fires while the
    event loop is busy is simply missed, and the window is lost with nothing
    anywhere to indicate it happened. This form re-answers the question on every
    pass, so a loop that was blocked straight through the activation instant still
    sees the window as open on its next pass and opens it late rather than never.
    """
    return activation_ts(close_ts, offset_seconds) <= now < close_ts


def cancel_ts(close_ts: int, cancel_lead_ms: int) -> float:
    """Instant at which the cancellation sweep begins.

    Crossing this moves the market to phase CANCELLING, and the Risk Engine's
    existing phase check denies new submissions from then on. That is a phase
    gate, not a timing gate: no code compares a clock to decide whether a window
    is "too late" (A10/D1).
    """
    return close_ts - (cancel_lead_ms / 1000.0)


def format_countdown(now: float, close_ts: int) -> str:
    """MM:SS remaining, floored, clamped at 00:00, never negative.

    Floored to match the Polymarket page: with 299.4 seconds left the venue shows
    04:59, and a countdown that rounded would show 05:00 and disagree with the
    screen the operator is comparing against. Clamped because a market that has
    closed but not yet settled would otherwise render a negative timer.
    """
    remaining = close_ts - now
    if remaining <= 0:
        return "00:00"
    total_seconds = int(remaining)
    return f"{total_seconds // 60:02d}:{total_seconds % 60:02d}"


def settlement_window_start(
    close_ts: int, window_seconds: int = SETTLEMENT_WINDOW_SECONDS
) -> int:
    """Start of the venue's settlement averaging window.

    Assumes the window sits [close_ts - w, close_ts] rather than straddling close.
    That placement is UNDOCUMENTED (A8/U1) and this value therefore feeds no
    decision anywhere: it is used only to record settlement_twap observationally.
    Recording it from day one is what will let the question be answered from real
    post-Aug-7 data instead of guessed.
    """
    return close_ts - window_seconds


def settlement_determined_fraction(
    seconds_before_close: float, window_seconds: int = SETTLEMENT_WINDOW_SECONDS
) -> Decimal:
    """Fraction of the settlement average already arithmetically fixed (A7).

        determined_fraction = (w - t) / w,  clamped to [0, 1]

    At t=15 half the outcome is already decided; at t=3, ninety percent. This is
    why the later windows are the better-informed ones rather than the reckless
    ones — what remains at t=3 is a liquidity question, not an information one.

    Returned as Decimal, and used for display only. No trading decision reads it.
    """
    if window_seconds <= 0:
        raise ValueError(f"settlement window must be positive, got {window_seconds}")
    t = to_decimal(str(seconds_before_close))
    w = to_decimal(window_seconds)
    fraction = (w - t) / w
    if fraction <= 0:
        return Decimal("0")
    if fraction >= 1:
        return Decimal("1")
    return fraction


def windows_by_priority(offsets: tuple[int, ...]) -> tuple[int, ...]:
    """Execution windows in priority order: 3, 5, 7, 10, 15.

    Ascending offset — the window closest to close is tried first because it is
    the best-informed one, having the largest determined fraction behind it.
    """
    return tuple(sorted(offsets))
