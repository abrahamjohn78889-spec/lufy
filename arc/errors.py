"""Error taxonomy for ARC.

Two families, and the distinction decides whether the process lives or dies:

    ArcFatalError    the process must not continue. Raised only for configuration
                     and startup invariants. Exits non-zero.
    ArcError         operational. The market or the order is affected; the process
                     keeps running, keeps its feed, keeps its dashboard.

Documentation uncertainty is NOT fatal (A8). A bad config is.
"""

from __future__ import annotations

__all__ = [
    "ArcError",
    "ArcFatalError",
    "BindAddressError",
    "CancelAckTimeoutError",
    "ConfigInvariantError",
    "ConnectionLostError",
    "FeedError",
    "MarketPhaseError",
    "NoDirectionError",
    "ObservationRejectedError",
    "PostOnlyWouldCrossError",
    "PriceToBeatUnavailableError",
    "SchemaMigrationError",
    "StorageError",
    "TransientLatencyRejectError",
    "WindowFreezeError",
]


class ArcFatalError(Exception):
    """The process cannot safely continue. Always exits non-zero.

    Reserved for conditions where continuing would mean trading under a
    configuration nobody authorised. Never raised for market conditions,
    network failures, or unverified venue documentation.
    """


class ConfigInvariantError(ArcFatalError):
    """A configuration invariant was violated.

    Fatal rather than a warning because every one of these invariants describes a
    configuration that would keep running and look healthy while behaving
    differently from what the operator configured — a window with no buffer never
    fires, an entry band narrower than the tick size admits no price at all.
    """


class BindAddressError(ArcFatalError):
    """The dashboard was asked to bind a non-loopback address.

    There is no authentication anywhere in this codebase (A3); the loopback bind
    IS the access control. Binding 0.0.0.0 would expose an unauthenticated
    trading control surface to the internet.
    """


class SchemaMigrationError(ArcFatalError):
    """The database schema could not be brought to the expected version.

    Fatal because running against a half-migrated schema would write frozen
    window values into columns that may not exist, losing them silently and
    breaking verbatim restart recovery (A4).
    """


class ArcError(Exception):
    """Operational failure. The process continues."""


class StorageError(ArcError):
    """A database operation failed."""


class FeedError(ArcError):
    """The market data feed failed or produced an unusable payload."""


class ConnectionLostError(ArcError):
    """The connection dropped with operations in flight.

    Those operations have an INDETERMINATE outcome. They are never blind-retried:
    a retry of an order that in fact reached the venue double-fills (A14). They
    are resolved by reconciliation against the venue's own order list.
    """


class TransientLatencyRejectError(ArcError):
    """The venue returned "Global Rate Limit Exceeded".

    Despite the wording this is a transient latency reject, not a rate limit, and
    it is retried WITHOUT backoff. Backing off here spends the remaining
    milliseconds of a 3-second window waiting and loses the window entirely (A14).
    """


class PostOnlyWouldCrossError(ArcError):
    """The venue refused a post-only order because it would have crossed the spread.

    TERMINAL for that submission, and deliberately not retried. The limit price is
    not this layer's to change: it was frozen upstream from the frozen PTB, the
    frozen signal TWAP, the frozen direction, the locked trigger and the configured
    buffer, inside an immutable ExecutionIntent. Retrying the same intent would
    re-cross for the same reason; retrying a DIFFERENT price would be the execution
    layer inventing a trading decision nobody approved, and the resulting fill would
    look exactly like an approved one afterwards.

    So the rejection is recorded, persisted and displayed, and the market keeps
    being monitored under the original decision, unchanged. Any repricing,
    replacement or operator-driven recovery policy for this condition has to arrive
    as an explicit specification; it is not inferred here.
    """


class CancelAckTimeoutError(ArcError):
    """A cancel was not acknowledged within CANCEL_ACK_TIMEOUT_MS.

    The order becomes INDETERMINATE and counts as LIVE until reconciliation says
    otherwise. Recording it as cancelled would be a claim the bot cannot support,
    and if the order is actually resting it rides into settlement unhedged (A13).
    """


class PriceToBeatUnavailableError(ArcError):
    """The official Price To Beat could not be obtained from market metadata.

    The market is marked DEAD and is not traded. There is no fallback: PTB is
    never calculated, estimated, derived from spot, or interpolated (A1 Rule 1).
    """


class MarketPhaseError(ArcError):
    """An operation was attempted in a market phase that forbids it."""


class ObservationRejectedError(ArcError):
    """An observation was refused by a market instance.

    Raised when a DEAD or SETTLED market is offered an observation. Accepting one
    would move a signal TWAP whose window has already closed.
    """


class WindowFreezeError(ArcError):
    """An execution window freeze was rejected.

    A freeze is all-or-nothing. A partially frozen window keeps a real
    opening_twap next to a defaulted buffer, producing a locked trigger that was
    never configured, and nothing downstream can tell the difference (A12).
    """


class NoDirectionError(ArcError):
    """The frozen TWAP equalled the official PTB exactly, so no direction exists.

    Deliberately NOT a WindowFreezeError. A freeze rejection is RETRYABLE — the
    usual cause is that no observation has arrived yet, and the level-triggered
    pass leaves the window PENDING and tries again on the next tick. This is the
    opposite: direction is determined exactly once, at the window's opening
    instant, and equality at that instant is a final verdict. If it were folded
    into WindowFreezeError the window would be retried on every subsequent pass and
    would freeze a direction from a LATER TWAP — the exact recalculation the
    strict-comparison contract forbids.
    """

