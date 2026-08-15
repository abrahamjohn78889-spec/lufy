"""Repricing: cancel, then place. Never amend.

An amend is a single venue call with two outcomes and no way to tell which one
happened when the connection drops mid-flight — the old order may still be resting,
or the new one may be, or both. Cancel-then-place has one in-flight operation at a
time, so a failure at any point leaves a state reconciliation can resolve.

The rule that makes this safe: the replacement is placed ONLY after the cancel is
acknowledged. An unacknowledged cancel means the original may still be on the book,
and placing the replacement then would leave two live orders for one submission
slot — double exposure, from a function whose purpose is to keep exposure constant.

Repricing never changes the size or the side. It changes only the resting price,
one tick per retry, and it is optional: the engine works correctly with repricing
switched off entirely (spec §11 — the MAJORITY price-retry switch).

PRICE RETRY (spec §11). When enabled, an unfilled resting order may be retried
at a better price: UP orders step +1 valid price tick, DOWN orders step -1 tick.
Price only, never quantity. The step keeps the same MAJORITY direction, market
and window, stays inside the entry band, is quantized to the venue tick, and is
capped at MAX_PRICE_RETRIES generations per chain. Cancel-then-place means a
retry can never leave a duplicate live order.

FILL PRIORITY (final spec §19/§20). The same price is tried FIRST: a resting
order gets a configurable number of polling passes — `pre_reprice_attempts`,
range 5-10, default 5 — at its current price before any cancel is considered.
Only afterwards may the order move one tick. The counter resets when a
successor is placed, so every resting price gets its own set of attempts.
"""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Final

from arc.domain.enums import Direction, OrderState
from arc.domain.models import Order
from arc.domain.money import dec_str
from arc.execution.orders import new_order, next_generation_id, transition
from arc.execution.protocol import Executor
from arc.execution.ratelimit import TokenBucket
from arc.execution.retry import Disposition, classify, rejection_reason
from arc.logging_setup import log_event
from arc.storage.store import Store

__all__ = ["MAX_PRICE_RETRIES", "RepricePolicy", "Repricer"]

# Spec §11: the maximum number of price retries per order chain. The original
# order is generation 0; each retry is one more. Once the chain has produced
# this many successors it stays where it rests — bounded by construction.
MAX_PRICE_RETRIES: Final[int] = 3


class RepricePolicy:
    """When a resting order should move. Pure arithmetic over prices.

    Spec §11: one valid price tick per retry, direction decides the sign — UP
    steps +1 tick, DOWN steps -1 tick — only when the book's best has moved off
    the resting price. Bounded by the same entry band the decision was validated
    against, so a retry can never carry the resting price past a limit the
    operator set. A reprice that walked outside the band would be an order the
    risk gates never approved, placed by a component that is not allowed to make
    that judgement.
    """

    __slots__ = ("_band_max", "_band_min", "_tick")

    def __init__(self, *, band_min: Decimal, band_max: Decimal, tick: Decimal) -> None:
        self._band_min = band_min
        self._band_max = band_max
        self._tick = tick

    def target(
        self, current: Decimal, best: Decimal | None, direction: Direction
    ) -> Decimal | None:
        """The price to move to, or None to stay put.

        Moves only when the book's best has moved off the resting price (the
        order is no longer at the front of the book), and then by exactly one
        tick in the direction §11 names. Never crosses the spread in a single
        step and never leaves the band.
        """
        if best is None or best == current:
            return None
        candidate = (
            current + self._tick if direction is Direction.UP else current - self._tick
        )
        if candidate <= Decimal("0"):
            return None
        if candidate < self._band_min or candidate > self._band_max:
            return None
        return candidate


class Repricer:
    """Moves a resting order by cancelling it and placing its successor."""

    __slots__ = ("_bucket", "_executor", "_logger", "_passes", "_policy", "_pre_attempts", "_store")

    def __init__(
        self,
        store: Store,
        executor: Executor,
        policy: RepricePolicy,
        *,
        bucket: TokenBucket,
        # Final spec §20: configurable pre-repricing attempt count, range 5-10.
        # The runtime passes the configured value; the default is the safe end of
        # the range (more same-price attempts, fewer cancels).
        pre_reprice_attempts: int = 5,
        logger: logging.Logger | None = None,
    ) -> None:
        self._store = store
        self._executor = executor
        self._policy = policy
        self._bucket = bucket
        self._logger = logger
        self._pre_attempts = pre_reprice_attempts
        # Polling passes seen per order chain (final spec §19/§20). A resting
        # order keeps its price for the first `_pre_attempts` passes; only then
        # is a move considered. Reset when a successor is placed, pruned when
        # the chain leaves the live states, so the dict tracks live chains only.
        self._passes: dict[str, int] = {}

    async def maybe_reprice(self, order: Order, now: float) -> Order:
        """Reprice `order` if the book has moved. Returns the live order either way.

        Returns the ORIGINAL order unchanged on any failure, including an
        unacknowledged cancel, because the original may still be resting and the
        caller must keep treating it as live.
        """
        if order.state not in (OrderState.SUBMITTED, OrderState.PARTIAL):
            self._passes.pop(order.reprice_chain_id, None)
            return order

        # Spec §11: at most MAX_PRICE_RETRIES per chain. The original order is
        # generation 0; the successor about to be built carries _generation_of + 1.
        if self._generation_of(order) > MAX_PRICE_RETRIES:
            self._passes.pop(order.reprice_chain_id, None)
            return order

        # Final spec §19/§20: the same price is tried first. For the first
        # `_pre_attempts` passes the order rests untouched, whatever the book is
        # doing; only afterwards is a move considered on this chain.
        passes = self._passes.get(order.reprice_chain_id, 0) + 1
        self._passes[order.reprice_chain_id] = passes
        if passes <= self._pre_attempts:
            return order

        best = await self._best(order.market_slug, order.direction)
        target = self._policy.target(order.price, best, order.direction)
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
            # Carried from the order being replaced, never defaulted. A successor
            # that lost its owner would be derived under the default owner instead,
            # so it would take an id belonging to a different chain — colliding with
            # that chain and escaping every owner-scoped sweep thereafter.
            engine=order.engine,
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
        # The successor rests at a new price: give it its own set of same-price
        # attempts before any further move (final spec §19/§20).
        self._passes[order.reprice_chain_id] = 0
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
