"""The venue interface. One Protocol, two adapters, no third path.

V1 (paper) and V2 (live) differ ONLY here. Everything above this file — submission,
the order FSM, fill monitoring, repricing, the sweep and reconciliation — runs
byte-identical in both modes, which is what makes a paper run evidence about the
live one rather than a separate program that happens to look similar.

Every method takes the values it needs as arguments and reads no ambient state. In
particular nothing here reads a clock: `now` arrives from the caller (A10/D1).
"""

from __future__ import annotations

from collections.abc import Awaitable
from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol, runtime_checkable

from arc.domain.enums import Direction, Mode
from arc.domain.models import Fill, Order

__all__ = ["Executor", "VenueOrder"]


@dataclass(frozen=True, slots=True)
class VenueOrder:
    """What the venue says about one order. The authority during reconciliation.

    Carries `client_order_id` because that is the only field ARC controls, and
    reconciliation after a connection loss has to match venue rows back to local
    rows without trusting a venue id that may never have been received.
    """

    venue_order_id: str
    client_order_id: str
    price: Decimal
    size: Decimal
    filled_size: Decimal
    resting: bool


@runtime_checkable
class Executor(Protocol):
    """Places, cancels and reports orders. Knows nothing about why they exist."""

    @property
    def mode(self) -> Mode: ...

    def place(self, order: Order) -> Awaitable[str]:
        """Submit `order`. Returns the venue order id.

        Raises ConnectionLostError when the outcome is unknown. It must never
        raise that for a request that provably never left, because the caller
        responds by marking the order INDETERMINATE rather than retrying (A14).
        """

    def cancel(self, order: Order) -> Awaitable[None]:
        """Cancel `order`. Raises CancelAckTimeoutError if no acknowledgement."""

    def open_orders(self, market_slug: str) -> Awaitable[tuple[VenueOrder, ...]]:
        """Every order the venue still considers live for this market."""

    def fills(self, market_slug: str) -> Awaitable[tuple[Fill, ...]]:
        """Every fill the venue has recorded for this market, from the beginning.

        Deliberately not "since last poll": a cursor lost across a restart would
        lose the fills that arrived while the process was down. The whole list is
        cheap at five-minute market scale and de-duplication is already exact,
        because fills are stored INSERT OR IGNORE on the venue's own fill id.
        """

    def best_price(self, market_slug: str, direction: Direction) -> Awaitable[Decimal | None]:
        """Best resting bid on the given side, or None when the book is empty."""

    def mark_price(self, market_slug: str, direction: Direction) -> Decimal | None:
        """Synchronous best price for marking an open position.

        The same value `best_price` would return, but readable without an await so
        a synchronous payload builder (the status document) can mark live P&L. None
        when no price exists rather than zero — a zero would read as "no risk".
        """
