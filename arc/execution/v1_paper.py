"""V1 paper adapter. A deterministic simulated venue, in memory.

This is not a mock and it is not demo code: it is the V1 execution mode, and the
engine above it is byte-identical to the one that runs V2. What it simulates is the
venue's BOOK BEHAVIOUR, and it simulates it conservatively.

It never invents a fill. A passive limit order fills only when a counterparty
actually trades through its price, so this adapter fills only when the caller
supplies a real observed trade via `trade()`. An adapter that filled every order at
submission would make every paper run report a 100% fill rate and would hide the
single most important execution risk there is — that a passive order at the wrong
price simply never fills, which is the unfilled-at-settlement outcome the engine
has to handle correctly.

Nothing here reads a clock; every timestamp arrives from the caller (A10/D1).
"""

from __future__ import annotations

import itertools
from decimal import Decimal
from typing import Final

from arc.domain.enums import Direction, Mode
from arc.domain.models import Fill, Order
from arc.errors import ArcError
from arc.execution.protocol import VenueOrder

__all__ = ["PaperExecutor"]

_ZERO: Final[Decimal] = Decimal("0")


class PaperExecutor:
    """In-memory venue. Same interface, same sequencing, no network."""

    __slots__ = ("_books", "_fills", "_resting", "_sequence")

    def __init__(self) -> None:
        self._resting: dict[str, VenueOrder] = {}
        self._fills: dict[str, list[Fill]] = {}
        self._books: dict[tuple[str, Direction], Decimal] = {}
        self._sequence = itertools.count(1)

    @property
    def mode(self) -> Mode:
        return Mode.V1

    # ── the Executor interface ───────────────────────────────────────────────

    async def place(self, order: Order) -> str:
        if order.order_id in self._resting:
            # The venue's own duplicate-client-id refusal. Reproduced here because
            # it is the last line of defence against a double submission, and a
            # paper mode that accepted duplicates would never exercise it.
            raise ArcError(f"duplicate client order id {order.order_id}")
        venue_id = f"paper-{next(self._sequence)}"
        self._resting[order.order_id] = VenueOrder(
            venue_order_id=venue_id,
            client_order_id=order.order_id,
            price=order.price,
            size=order.size,
            filled_size=order.filled_size,
            resting=True,
        )
        return venue_id

    async def cancel(self, order: Order) -> None:
        existing = self._resting.get(order.order_id)
        if existing is None:
            raise ArcError(f"no resting order {order.order_id}")
        self._resting.pop(order.order_id)

    async def open_orders(self, market_slug: str) -> tuple[VenueOrder, ...]:
        prefix = f"{market_slug}:"
        return tuple(
            v for k, v in sorted(self._resting.items()) if k.startswith(prefix)
        )

    async def fills(self, market_slug: str) -> tuple[Fill, ...]:
        return tuple(self._fills.get(market_slug, ()))

    async def best_price(self, market_slug: str, direction: Direction) -> Decimal | None:
        return self._books.get((market_slug, direction))

    # ── simulation controls ──────────────────────────────────────────────────

    def quote(self, market_slug: str, direction: Direction, price: Decimal) -> None:
        """Set the best resting price on one side of the book."""
        self._books[(market_slug, direction)] = price

    def trade(self, market_slug: str, price: Decimal, size: Decimal) -> tuple[Fill, ...]:
        """A counterparty trades `size` at `price`. Fills whoever it crosses.

        Matched in client-order-id order, which is deterministic because the ids
        themselves are derived rather than generated. Price-time priority would be
        more realistic and less reproducible; reproducibility is worth more here,
        because a paper run that cannot be replayed cannot be used as evidence.
        """
        produced: list[Fill] = []
        remaining = size
        for client_id in sorted(self._resting):
            if remaining <= _ZERO:
                break
            resting = self._resting[client_id]
            if not client_id.startswith(f"{market_slug}:"):
                continue
            # A passive buy fills when someone sells at or below its limit.
            if price > resting.price:
                continue
            available = resting.size - resting.filled_size
            if available <= _ZERO:
                continue
            traded = min(available, remaining)
            remaining -= traded
            filled = resting.filled_size + traded
            fill = Fill(
                fill_id=f"{client_id}#{filled}",
                order_id=client_id,
                market_slug=market_slug,
                size=traded,
                price=resting.price,
                ts=0.0,
            )
            produced.append(fill)
            self._fills.setdefault(market_slug, []).append(fill)
            if filled >= resting.size:
                self._resting.pop(client_id)
            else:
                self._resting[client_id] = VenueOrder(
                    venue_order_id=resting.venue_order_id,
                    client_order_id=client_id,
                    price=resting.price,
                    size=resting.size,
                    filled_size=filled,
                    resting=True,
                )
        return tuple(produced)
