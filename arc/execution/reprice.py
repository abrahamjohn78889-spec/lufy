"""Repricing: cancel, then place. Never amend.

An amend is a single venue call with two outcomes and no way to tell which one
happened when the connection drops mid-flight — the old order may still be resting,
or the new one may be, or both. Cancel-then-place has one in-flight operation at a
time, so a failure at any point leaves a state reconciliation can resolve.

The rule that makes this safe: the replacement is placed ONLY after the cancel is
acknowledged. An unacknowledged cancel means the original may still be on the book,
and placing the replacement then would leave two live orders for one submission
slot — double exposure, from a function whose purpose is to keep exposure constant.

Repricing never changes the size or the side. It changes only the resting price, to
follow the book while the order waits, and it is optional: the engine works
correctly with repricing switched off entirely.
"""

from __future__ import annotations

import logging
from decimal import Decimal

from arc.domain.enums import Direction, OrderState
from arc.domain.models import Order
from arc.domain.money import dec_str, quantize_price
from arc.execution.orders import new_order, next_generation_id, transition
from arc.execution.protocol import Executor
from arc.execution.ratelimit import TokenBucket
from arc.execution.retry import Disposition, classify, rejection_reason
from arc.logging_setup import log_event
from arc.storage.store import Store

__all__ = ["RepricePolicy", "Repricer"]


class RepricePolicy:
    """When a resting order should move. Pure arithmetic over prices.

    Bounded by the same entry band the decision was validated against, so following
    the book can never carry the resting price past a limit the operator set. A
    reprice that walked outside the band would be an order the risk gates never
    approved, placed by a component that is not allowed to make that judgement.
    """

    __slots__ = ("_band_max", "_band_min", "_tick")

    def __init__(self, *, band_min: Decimal, band_max: Decimal, tick: Decimal) -> None:
        self._band_min = band_min
        self._band_max = band_max
        self._tick = tick

    def target(self, current: Decimal, best: Decimal | None) -> Decimal | None:
        """The price to move to, or None to stay put.

        Joins the best resting bid rather than crossing the spread: the whole
        posture is passive, and a crossing order pays the spread on every window.
        """
        if best is None:
            return None
        candidate = quantize_price(best, self._tick)
        if candidate == current:
            return None
        if candidate < self._band_min or candidate > self._band_max:
            return None
        return candidate


class Repricer:
    """Moves a resting order by cancelling it and placing its successor."""

    __slots__ = ("_bucket", "_executor", "_logger", "_policy", "_store")

    def __init__(
        self,
        store: Store,
        executor: Executor,
        policy: RepricePolicy,
        *,
        bucket: TokenBucket,
        logger: logging.Logger | None = None,
    ) -> None:
        self._store = store
        self._executor = executor
        self._policy = policy
        self._bucket = bucket
        self._logger = logger

    async def maybe_reprice(self, order: Order, now: float) -> Order:
        """Reprice `order` if the book has moved. Returns the live order either way.

        Returns the ORIGINAL order unchanged on any failure, including an
        unacknowledged cancel, because the original may still be resting and the
        caller must keep treating it as live.
        """
        if order.state not in (OrderState.SUBMITTED, OrderState.PARTIAL):
            return order

        best = await self._best(order.market_slug, order.direction)
        target = self._policy.target(order.price, best)
        if target is None:
            return order

        # Cancels bypass the token bucket (A4): a cancel that waits for a token is
        # a cancel that may not complete, and the order it was retracting stays live.
        try:
            await self._executor.cancel(order)
        except Exception as exc:
            if classify(exc) is Disposition.INDETERMINATE:
                transition(order, OrderState.INDETERMINATE, now, str(exc))
                self._store.save_order(order)
                log_event(
                    logging.WARNING,
                    "Reprice Abandoned",
                    f"{order.order_id}  cancel unacknowledged; original may rest",
                    logger=self._logger,
                )
            return order

        transition(order, OrderState.CANCELLED, now, "reprice")
        self._store.save_order(order)

        # The replacement carries only the UNFILLED remainder. Re-placing the full
        # size would add back quantity that has already executed, so a partially
        # filled order that repriced twice would end up holding more than approved.
        remainder = order.remaining_size
        if remainder <= Decimal("0"):
            return order

        successor = new_order(
            market_slug=order.market_slug,
            offset_seconds=order.offset_seconds,
            index=self._index_of(order),
            generation=self._generation_of(order),
            direction=order.direction,
            price=target,
            size=remainder,
            now=now,
            trace_id=order.trace_id,
        )
        self._store.save_order(successor)

        await self._bucket.acquire(now)
        try:
            successor.venue_order_id = await self._executor.place(successor)
        except Exception as exc:
            state = (
                OrderState.INDETERMINATE
                if classify(exc) is Disposition.INDETERMINATE
                else OrderState.REJECTED
            )
            transition(successor, state, now, rejection_reason(exc))
            self._store.save_order(successor)
            return successor

        transition(successor, OrderState.SUBMITTED, now)
        self._store.save_order(successor)
        log_event(
            logging.INFO,
            "Order Repriced",
            f"{order.order_id} -> {successor.order_id}  "
            f"{dec_str(order.price)} -> {dec_str(target)}",
            logger=self._logger,
        )
        return successor

    async def _best(self, market_slug: str, direction: Direction) -> Decimal | None:
        try:
            return await self._executor.best_price(market_slug, direction)
        except Exception:
            # Staying put on an unreadable book is the safe default: the resting
            # order is already valid, and cancelling it to chase a price nobody
            # could read would give up queue position for nothing.
            return None

    @staticmethod
    def _index_of(order: Order) -> int:
        return int(order.reprice_chain_id.rsplit(":", 1)[1])

    @staticmethod
    def _generation_of(order: Order) -> int:
        return int(next_generation_id(order).rsplit(":", 1)[1])
