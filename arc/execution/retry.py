"""Retry classification. Four failures, four different correct responses (A14).

    TransientLatencyRejectError   retry IMMEDIATELY, no backoff
    ConnectionLostError           NEVER retry; the order is INDETERMINATE
    CancelAckTimeoutError         never retry the cancel; INDETERMINATE, counts LIVE
    PostOnlyWouldCrossError       terminal for that submission; never repriced

The middle two are the whole reason this module exists as a classifier rather than
as a `try/except: retry` at each call site. A submission that raised on a dropped
connection may well have reached the venue, so a retry double-fills — and the
duplicate is invisible afterwards, because both orders are genuine.

The first is counter-intuitive and easy to "fix" wrongly: the venue's message says
"Global Rate Limit Exceeded" but it is a transient latency reject, and an
exponential backoff spends the remaining milliseconds of a 3-second window asleep.

The last is the one that looks most retryable and is not. A post-only order the
venue refused for crossing would be accepted at a different price — but that price
is not this layer's to choose. It was frozen upstream, and an execution layer that
picks its own is no longer executing the approved decision.
"""

from __future__ import annotations

from enum import StrEnum

from arc.domain.enums import POST_ONLY_WOULD_CROSS_REASON
from arc.errors import (
    ArcError,
    CancelAckTimeoutError,
    ConnectionLostError,
    PostOnlyWouldCrossError,
    TransientLatencyRejectError,
)

__all__ = [
    "IMMEDIATE_RETRY_LIMIT",
    "Disposition",
    "classify",
    "rejection_reason",
]

# Bounded so a venue stuck in a permanent transient-reject state cannot spin
# forever inside a window. Six immediate attempts against a venue answering in
# single-digit milliseconds still costs well under the shortest window.
IMMEDIATE_RETRY_LIMIT = 6


class Disposition(StrEnum):
    """What to do with a failed venue call.

    RETRY_NOW      re-issue immediately, no delay
    INDETERMINATE  outcome unknown; resolve by reconciliation, never by retrying
    FAIL           a definite, final refusal
    """

    RETRY_NOW = "RETRY_NOW"
    INDETERMINATE = "INDETERMINATE"
    FAIL = "FAIL"


def classify(exc: BaseException) -> Disposition:
    """Map a failure to its disposition.

    Order matters only for readability; the three types are disjoint.
    """
    if isinstance(exc, TransientLatencyRejectError):
        return Disposition.RETRY_NOW
    if isinstance(exc, ConnectionLostError | CancelAckTimeoutError):
        return Disposition.INDETERMINATE
    if isinstance(exc, PostOnlyWouldCrossError):
        # Definite and TERMINAL for this submission. Stated separately from the
        # ArcError catch-all below only to make the ruling explicit: this is the
        # one failure where a retry is technically possible and still forbidden,
        # because the only retry that could succeed is one at a different price,
        # and the price belongs to a frozen decision this layer may not rewrite.
        return Disposition.FAIL
    if isinstance(exc, ArcError):
        return Disposition.FAIL
    # Anything unrecognised — a venue SDK's own exception type, an OSError from the
    # socket layer — is treated as unknown rather than as a definite failure.
    # Calling it FAIL would mark an order dead that may be resting on the book.
    return Disposition.INDETERMINATE


def rejection_reason(exc: BaseException) -> str:
    """The reason string persisted on a rejected order.

    A post-only cross gets a dedicated, stable code rather than the venue's prose,
    so the database and the dashboard can identify it exactly instead of matching
    on a message the venue is free to reword. Everything else keeps its own text,
    which is all the operator has for a one-off failure.
    """
    if isinstance(exc, PostOnlyWouldCrossError):
        return POST_ONLY_WOULD_CROSS_REASON
    return str(exc)
