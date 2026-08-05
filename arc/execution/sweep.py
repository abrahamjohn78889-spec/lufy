"""The settlement sweep. Every live order is retracted before the market closes.

An order still resting when the market settles is an uncontrolled position: it can
fill against a book that already knows the outcome, at a price that was set when
the outcome was still open. The sweep is what makes "unfilled at close" a clean
terminal state instead of a race.

Cancels bypass the outbound token bucket (A4). Throttling the sweep would mean the
bucket decides how much exposure survives into settlement, which inverts the point
of both mechanisms.

An unacknowledged cancel is NOT recorded as cancelled. It becomes INDETERMINATE and
keeps counting as live until reconciliation resolves it against the venue (A13):
writing "cancelled" would be a claim the bot cannot support, and if the order is in
fact resting, nothing would ever look for it again.

The sweep is driven by market PHASE, not by a clock (A10/D1). The caller moves the
market to CANCELLING; this module never asks what time it is.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from arc.domain.enums import OrderState
from arc.domain.models import Order
from arc.execution.orders import transition
from arc.execution.protocol import Executor
from arc.execution.retry import Disposition, classify
from arc.logging_setup import log_event
from arc.storage.store import Store

__all__ = ["SweepResult", "Sweeper"]


@dataclass(frozen=True, slots=True)
class SweepResult:
    """Outcome of one sweep. `unknown` is the number needing reconciliation."""

    market_slug: str
    cancelled: tuple[str, ...]
    unknown: tuple[str, ...]

    @property
    def clean(self) -> bool:
        """True when nothing is left in an unresolved state."""
        return not self.unknown


class Sweeper:
    """Cancels every live order for a market."""

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

    async def sweep(self, market_slug: str, now: float) -> SweepResult:
        """Retract everything still live. Safe to call repeatedly.

        Idempotent because it re-reads live orders from the store on each call: an
        order cancelled by an earlier sweep is no longer live and is not retried,
        and one left INDETERMINATE is still live and IS retried.
        """
        cancelled: list[str] = []
        unknown: list[str] = []

        for order in self._store.live_orders(market_slug):
            if await self._cancel_one(order, now):
                cancelled.append(order.order_id)
            else:
                unknown.append(order.order_id)

        if cancelled or unknown:
            log_event(
                logging.INFO,
                "Sweep Complete",
                f"{market_slug}  {len(cancelled)} cancelled  {len(unknown)} unknown",
                logger=self._logger,
            )
        return SweepResult(
            market_slug=market_slug,
            cancelled=tuple(cancelled),
            unknown=tuple(unknown),
        )

    async def _cancel_one(self, order: Order, now: float) -> bool:
        if order.state is OrderState.PENDING:
            # Never reached the venue. There is nothing to cancel, and calling the
            # venue for an id it has never seen produces a spurious error.
            transition(order, OrderState.EXPIRED, now, "swept before submission")
            self._store.save_order(order)
            return True

        try:
            await self._executor.cancel(order)
        except Exception as exc:
            if classify(exc) is Disposition.FAIL:
                # A definite refusal: the venue says this order is not cancellable,
                # which means it is already gone. Recording it as cancelled is
                # accurate, unlike the unknown case below.
                transition(order, OrderState.CANCELLED, now, str(exc))
                self._store.save_order(order)
                return True
            if order.state is not OrderState.INDETERMINATE:
                transition(order, OrderState.INDETERMINATE, now, str(exc))
                self._store.save_order(order)
            log_event(
                logging.WARNING,
                "Cancel Unknown",
                f"{order.order_id}  {exc}",
                logger=self._logger,
            )
            return False

        transition(order, OrderState.CANCELLED, now, "settlement sweep")
        self._store.save_order(order)
        return True
