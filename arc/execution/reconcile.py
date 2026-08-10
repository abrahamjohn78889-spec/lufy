"""Reconciliation. The venue is the authority; local state is a cache of it.

Every INDETERMINATE order exists because ARC lost the answer to a question it had
already asked. The only correct way to resolve one is to ask the venue what it
holds — never to guess, never to retry the original request, and never to assume
the safer-sounding outcome, because "assume cancelled" leaves a live order nobody
is watching and "assume live" cancels an id the venue does not recognise.

Matching is by client order id, not by venue id. A submission whose response never
arrived has no venue id locally, and that is exactly the case reconciliation exists
to handle. Client ids are derived (see orders.py), so they are known before the
request is sent and survive any failure of it.

Reconciliation also runs on a clean restart, not only after an error. A process
that came back up has no in-memory order state at all, and the difference between
"restarted" and "lost the connection" is not one the venue can see.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal

from arc.domain.enums import OrderState
from arc.domain.models import Order
from arc.execution.orders import transition
from arc.execution.protocol import Executor, VenueOrder
from arc.logging_setup import log_event
from arc.storage.store import Store

__all__ = ["ReconcileResult", "Reconciler"]


@dataclass(frozen=True, slots=True)
class ReconcileResult:
    """What reconciliation changed."""

    market_slug: str
    resolved: tuple[str, ...]
    still_live: tuple[str, ...]
    orphans: tuple[str, ...]

    @property
    def clean(self) -> bool:
        """True when nothing unknown and nothing unaccounted-for remains."""
        return not self.orphans


class Reconciler:
    """Brings local order state into agreement with the venue."""

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

    async def reconcile(
        self, market_slug: str, now: float, *, engine: str | None = None
    ) -> ReconcileResult:
        """Resolve every unknown order for this market against the venue.

        `engine` scopes the pass to one engine's orders. None means every engine,
        which is what recovery and the restart path want: a process that just came
        back holds no in-memory state for EITHER engine, and reconciling only one of
        them would leave the other's unknown orders unresolved and unwatched.

        An engine-scoped pass filters the local rows AND the orphan set. The orphan
        filter matters as much as the first: `known` is built from the rows this pass
        looked at, so without it every order belonging to the other engine would be
        absent from `known` and reported as an orphan resting at the venue with no
        local record — a loud, permanent, entirely false alarm that also blocks
        trading through the orphan gate.
        """
        venue = await self._executor.open_orders(market_slug)
        by_client = {v.client_order_id: v for v in venue}
        rows = self._store.orders_for(market_slug)
        local = tuple(o for o in rows if engine is None or o.engine == engine)
        # Built from every row, never from the filtered subset, so an engine-scoped
        # pass cannot mistake the other engine's legitimate order for an orphan.
        known = {o.order_id for o in rows}

        resolved: list[str] = []
        still_live: list[str] = []

        for order in local:
            if order.state is not OrderState.INDETERMINATE:
                if order.is_live:
                    still_live.append(order.order_id)
                continue
            if self._resolve(order, by_client.get(order.order_id), now):
                resolved.append(order.order_id)
            else:
                still_live.append(order.order_id)

        # Orders the venue holds that ARC has no row for. This is the one condition
        # the bot cannot fix by itself — cancelling them blind could retract
        # someone else's order on a shared account — so they are surfaced loudly and
        # returned to the caller rather than silently swallowed.
        orphans = tuple(
            v.client_order_id or v.venue_order_id
            for v in venue
            if v.client_order_id not in known
        )
        for orphan in orphans:
            log_event(
                logging.ERROR,
                "Orphan Order",
                f"{market_slug}  {orphan} rests at the venue with no local record",
                logger=self._logger,
            )

        return ReconcileResult(
            market_slug=market_slug,
            resolved=tuple(resolved),
            still_live=tuple(still_live),
            orphans=orphans,
        )

    def _resolve(self, order: Order, venue: VenueOrder | None, now: float) -> bool:
        """Settle one unknown order. Returns True when it reached a definite state."""
        if venue is None:
            # Absent from the venue's live list. It either never arrived or has
            # already finished; either way it is not resting, so it cannot fill and
            # cannot ride into settlement. Recorded by what actually executed:
            # a partially filled order that vanished is FILLED for what it filled
            # and CANCELLED for the rest, and calling it CANCELLED outright would
            # erase quantity the bot genuinely holds.
            target = (
                OrderState.FILLED
                if order.filled_size >= order.size and order.size > Decimal("0")
                else OrderState.CANCELLED
            )
            transition(order, target, now, "reconciled: absent from venue")
            self._store.save_order(order)
            log_event(
                logging.INFO,
                "Order Reconciled",
                f"{order.order_id}  -> {target.value}",
                logger=self._logger,
            )
            return True

        # Still resting. The venue's filled quantity wins over the local figure,
        # because the fills that produced it may have arrived while ARC was down.
        order.venue_order_id = venue.venue_order_id or order.venue_order_id
        order.filled_size = min(order.size, venue.filled_size)
        if not venue.resting:
            target = (
                OrderState.FILLED
                if order.filled_size >= order.size
                else OrderState.CANCELLED
            )
            transition(order, target, now, "reconciled: no longer resting")
            self._store.save_order(order)
            return True

        target = (
            OrderState.PARTIAL if order.filled_size > Decimal("0") else OrderState.SUBMITTED
        )
        transition(order, target, now, "reconciled: resting at venue")
        self._store.save_order(order)
        log_event(
            logging.INFO,
            "Order Reconciled",
            f"{order.order_id}  still resting  -> {target.value}",
            logger=self._logger,
        )
        return False
