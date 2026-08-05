"""Fill monitoring. Submission and filling are separate states, never conflated.

A submitted order is not a position. The distinction has to be structural, because
every downstream number — quota consumption, concurrent-position counting, realised
P/L — is a statement about filled quantity, and a system that treats acceptance as
execution reports positions it does not hold.

Idempotence is exact, not best-effort: fills are keyed on the venue's own fill id
and stored INSERT OR IGNORE, so a websocket redelivery, a reconnect replay and a
post-restart re-poll of the full history all converge on the same set. That is why
the executor returns every fill for the market rather than a delta since a cursor —
a cursor lost in a crash loses the fills that arrived while the process was down.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal
from typing import Final

from arc.domain.models import Fill, Order
from arc.domain.money import dec_str
from arc.execution.orders import apply_fill
from arc.execution.protocol import Executor
from arc.logging_setup import log_event
from arc.storage.store import Store

__all__ = ["FillEngine", "FillReport"]

_ZERO: Final[Decimal] = Decimal("0")


@dataclass(frozen=True, slots=True)
class FillReport:
    """What one polling pass learned. Only genuinely new fills appear."""

    market_slug: str
    new_fills: tuple[Fill, ...]

    @property
    def filled_size(self) -> Decimal:
        return sum((f.size for f in self.new_fills), _ZERO)


class FillEngine:
    """Polls the venue for fills and folds them into local order state."""

    __slots__ = ("_executor", "_logger", "_store")

    def __init__(
        self,
        store: Store,
        executor: Executor,
        *,
        logger: logging.Logger | None = None,
    ) -> None:
        self._store = store
        self._executor = executor
        self._logger = logger

    async def poll(self, market_slug: str, now: float) -> FillReport:
        """One pass. Records new fills and advances the orders they belong to."""
        reported = await self._executor.fills(market_slug)
        return self.ingest(market_slug, reported, now)

    def ingest(
        self, market_slug: str, reported: tuple[Fill, ...], now: float
    ) -> FillReport:
        """Fold a batch of venue-reported fills into storage and order state.

        Separate from `poll` so the same code path serves the polling loop, a
        websocket push and post-restart recovery. Three sources, one merge rule.
        """
        orders = {o.order_id: o for o in self._store.orders_for(market_slug)}
        accepted: list[Fill] = []

        for fill in reported:
            if fill.size <= _ZERO:
                continue
            order = orders.get(fill.order_id)
            if order is None:
                # A fill for an order this process never wrote. It is still real
                # money, so it is recorded rather than dropped; reconciliation is
                # what explains where the order came from.
                if self._store.save_fill(fill):
                    accepted.append(fill)
                    log_event(
                        logging.WARNING,
                        "Fill Unlinked",
                        f"{fill.fill_id}  order {fill.order_id} unknown locally",
                        logger=self._logger,
                    )
                continue

            # The database arbitrates novelty, not an in-memory set: a set is empty
            # again after a restart and every historical fill would re-apply,
            # doubling the recorded position.
            if not self._store.save_fill(fill):
                continue

            accepted.append(fill)
            apply_fill(order, fill.size, now)
            self._store.save_order(order)
            log_event(
                logging.INFO,
                "Order Filled",
                f"{order.order_id}  {dec_str(fill.size)} @ {dec_str(fill.price)}  "
                f"({dec_str(order.filled_size)}/{dec_str(order.size)})",
                logger=self._logger,
            )

        return FillReport(market_slug=market_slug, new_fills=tuple(accepted))

    def filled_for_window(self, market_slug: str, offset_seconds: int) -> Decimal:
        """Cumulative filled quantity for a window, across its whole reprice chain.

        Summed over quantity rather than counted over orders: five sub-minimum
        fills are one position, not five, and counting them as five would consume a
        three-trade quota on a single window (hazard H4).
        """
        return self._store.filled_size_for_window(market_slug, offset_seconds)

    def unfilled(self, market_slug: str) -> tuple[Order, ...]:
        """Live orders with quantity still outstanding."""
        return tuple(
            o for o in self._store.live_orders(market_slug) if o.remaining_size > _ZERO
        )
