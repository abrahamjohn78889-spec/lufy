"""Retry classification. Three failures, three different correct responses (A14).

    TransientLatencyRejectError   retry IMMEDIATELY, no backoff
    ConnectionLostError           NEVER retry; the order is INDETERMINATE
    CancelAckTimeoutError         never retry the cancel; INDETERMINATE, counts LIVE

The middle one is the whole reason this module exists as a classifier rather than
as a `try/except: retry` at each call site. A submission that raised on a dropped
connection may well have reached the venue, so a retry double-fills — and the
duplicate is invisible afterwards, because both orders are genuine.

The first is counter-intuitive and easy to "fix" wrongly: the venue's message says
"Global Rate Limit Exceeded" but it is a transient latency reject, and an
exponential backoff spends the remaining milliseconds of a 3-second window asleep.
"""

from __future__ import annotations

from enum import StrEnum

from arc.errors import (
    ArcError,
    CancelAckTimeoutError,
    ConnectionLostError,
    TransientLatencyRejectError,
)

__all__ = ["IMMEDIATE_RETRY_LIMIT", "Disposition", "classify"]

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
    if isinstance(exc, ArcError):
        return Disposition.FAIL
    # Anything unrecognised — a venue SDK's own exception type, an OSError from the
    # socket layer — is treated as unknown rather than as a definite failure.
    # Calling it FAIL would mark an order dead that may be resting on the book.
    return Disposition.INDETERMINATE
