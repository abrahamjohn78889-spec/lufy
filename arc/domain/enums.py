"""Domain enumerations.

Every enum here is a str-enum so it stores as TEXT and compares as text, which
keeps the database readable and removes any chance of an integer ordinal changing
meaning when a member is inserted.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Final

__all__ = [
    "DISPLAYED_ORDER_STATES",
    "LIVE_ORDER_STATES",
    "ORDER_STATE_DISPLAY",
    "TERMINAL_ORDER_STATES",
    "DenialReason",
    "Direction",
    "MarketPhase",
    "Mode",
    "OrderState",
    "Outcome",
    "SettlementSpecStatus",
    "WindowState",
]


class Mode(StrEnum):
    """Execution mode. Exactly two exist.

    There is deliberately no TESTNET member. It is not a rejected value or a
    guarded branch — it does not exist, so "testnet prices drove a real-money
    order" is not a bug that can be written (A3).
    """

    V1 = "V1"  # paper
    V2 = "V2"  # live


class Direction(StrEnum):
    UP = "UP"
    DOWN = "DOWN"

    @property
    def opposite(self) -> Direction:
        return Direction.DOWN if self is Direction.UP else Direction.UP


class MarketPhase(StrEnum):
    """Lifecycle of one market instance.

    DISCOVERED   the slug exists on the grid; nothing has been fetched yet
    ACTIVE       PTB frozen, signal TWAP accumulating, windows may open
    CANCELLING   the sweep has begun. The ONLY execution boundary that exists
                 (A10/D1): new submissions are denied by phase, never by a clock
    SETTLING     closed; still accepting observations, awaiting venue resolution
    SETTLED      venue resolution recorded. Terminal
    DEAD         official PTB was unobtainable. Never traded (A1 Rule 1). Terminal
    """

    DISCOVERED = "DISCOVERED"
    ACTIVE = "ACTIVE"
    CANCELLING = "CANCELLING"
    SETTLING = "SETTLING"
    SETTLED = "SETTLED"
    DEAD = "DEAD"


class WindowState(StrEnum):
    """State of one execution window.

    PENDING       not yet activated, or a freeze was rejected and left it untouched
    FROZEN        all five values locked atomically; immutable, including across restart
    FIRED         the direction-appropriate trigger comparison passed
    EXPIRED       the market closed without the trigger passing
    NO_DIRECTION  the frozen TWAP equalled the official PTB exactly

    NO_DIRECTION is terminal and is NOT an error. Direction determination uses strict
    comparison only: TWAP > PTB is UP, TWAP < PTB is DOWN, and equality resolves to
    neither. The window freezes no direction, authorises no intent and submits no
    order. It is a distinct state rather than folded into EXPIRED because EXPIRED
    means "a real trigger existed and was never crossed" — a buffer that was too
    wide — while this means "no direction was determinable". An operator tuning
    buffers must be able to tell those apart.
    """

    PENDING = "PENDING"
    FROZEN = "FROZEN"
    FIRED = "FIRED"
    EXPIRED = "EXPIRED"
    NO_DIRECTION = "NO_DIRECTION"


class OrderState(StrEnum):
    """Internal order state. Five display values, one honest unknown (A13)."""

    PENDING = "PENDING"
    SUBMITTED = "SUBMITTED"
    PARTIAL = "PARTIAL"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"
    REJECTED = "REJECTED"
    INDETERMINATE = "INDETERMINATE"


# PENDING and SUBMITTED both read as "Working" — the operator cannot act on the
# difference. EXPIRED folds into "Cancelled" for the same reason. INDETERMINATE
# keeps its own label because it is the one state where the bot genuinely does not
# know, and displaying it as "Cancelled" would assert something unverified.
ORDER_STATE_DISPLAY: Final[dict[OrderState, str]] = {
    OrderState.PENDING: "Working",
    OrderState.SUBMITTED: "Working",
    OrderState.PARTIAL: "Partial",
    OrderState.FILLED: "Filled",
    OrderState.CANCELLED: "Cancelled",
    OrderState.EXPIRED: "Cancelled",
    OrderState.REJECTED: "Rejected",
    OrderState.INDETERMINATE: "Unknown ⚠",
}

DISPLAYED_ORDER_STATES: Final[tuple[str, ...]] = (
    "Working",
    "Partial",
    "Filled",
    "Cancelled",
    "Rejected",
    "Unknown ⚠",
)

# INDETERMINATE counts as LIVE. The safe assumption for an unacknowledged cancel is
# that the order is still resting: treating it as dead would let the sweep skip it
# and carry an unhedged position into settlement (A13).
LIVE_ORDER_STATES: Final[frozenset[OrderState]] = frozenset(
    {
        OrderState.PENDING,
        OrderState.SUBMITTED,
        OrderState.PARTIAL,
        OrderState.INDETERMINATE,
    }
)

TERMINAL_ORDER_STATES: Final[frozenset[OrderState]] = frozenset(
    {
        OrderState.FILLED,
        OrderState.CANCELLED,
        OrderState.EXPIRED,
        OrderState.REJECTED,
    }
)


class Outcome(StrEnum):
    """Settled outcome, as reported by the venue.

    UNRESOLVED exists because "the venue has not told us yet" and "the venue said
    DOWN" must never collapse into the same value. Outcome is never inferred from
    ARC's own signal TWAP; if they disagree the venue wins and the divergence is
    logged (A12).
    """

    UP = "UP"
    DOWN = "DOWN"
    UNRESOLVED = "UNRESOLVED"


class DenialReason(StrEnum):
    """Why the Risk Engine refused a submission.

    Every denial carries one of these into the log stream. There is deliberately
    no lead-time reason here: the lead-time gate is repealed entirely (A10/D1) and
    MARKET_CANCELLING is a PHASE gate, not a timing gate.

    Each gate owns exactly one member. Two gates sharing a reason would make a
    rejection log unactionable: the operator would know a trade was refused but
    not which of two independent conditions refused it.
    """

    TRADING_DISABLED_SPEC_UNVERIFIED = "TRADING_DISABLED_SPEC_UNVERIFIED"
    TRADING_PAUSED = "TRADING_PAUSED"
    MARKET_CANCELLING = "MARKET_CANCELLING"
    MARKET_DEAD = "MARKET_DEAD"
    MARKET_NOT_ACTIVE = "MARKET_NOT_ACTIVE"
    PTB_UNAVAILABLE = "PTB_UNAVAILABLE"
    WINDOW_NOT_TRIGGERED = "WINDOW_NOT_TRIGGERED"
    STRATEGY_DISABLED = "STRATEGY_DISABLED"
    ENTRY_PRICE_LIMIT = "ENTRY_PRICE_LIMIT"
    ENTRY_PRICE_BELOW_MIN = "ENTRY_PRICE_BELOW_MIN"
    SIZE_BELOW_EXCHANGE_MINIMUM = "SIZE_BELOW_EXCHANGE_MINIMUM"
    TRADE_QUOTA_EXHAUSTED = "TRADE_QUOTA_EXHAUSTED"
    OPPOSING_DIRECTION_BLOCKED = "OPPOSING_DIRECTION_BLOCKED"
    POSITION_LIMIT_REACHED = "POSITION_LIMIT_REACHED"
    LOSS_LIMIT_REACHED = "LOSS_LIMIT_REACHED"
    DUPLICATE_INTENT = "DUPLICATE_INTENT"
    INSUFFICIENT_BALANCE = "INSUFFICIENT_BALANCE"
    FEED_STALE = "FEED_STALE"
    CLOCK_DRIFT_CRITICAL = "CLOCK_DRIFT_CRITICAL"
    RUNTIME_UNHEALTHY = "RUNTIME_UNHEALTHY"
    NO_BOOK_LIQUIDITY = "NO_BOOK_LIQUIDITY"


class SettlementSpecStatus(StrEnum):
    """Result of automatic settlement-spec verification (A8).

    UNVERIFIED is the startup value and it disables trading. The process still
    boots, the dashboard still works, feeds still run and windows still open and
    record decisions — it simply never submits. Waiting produces the dataset
    instead of nothing.
    """

    UNVERIFIED = "UNVERIFIED"
    VERIFIED = "VERIFIED"
    FAILED = "FAILED"
