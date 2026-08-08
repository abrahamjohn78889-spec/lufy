"""Order submission. Consumes the intent verbatim and divides, never multiplies.

The engine receives an immutable intent that has already passed every gate and
acts on it as given: nothing here recalculates a price, re-derives a direction,
re-reads a market value, or re-runs a check. Those numbers were frozen at the
window's opening instant precisely so that submission — which happens later, by
however many milliseconds — cannot act on values that moved in between.

SPLITTING. `submission_count` divides the approved exposure into N independent
passive orders. It is NOT a multiplier. N orders of the full size would place N
times the exposure that the position budget, the concurrency cap and both loss
caps were evaluated against, and every one of those gates would still report a
pass — the over-exposure would only be visible in the position itself. So the
splits sum to exactly the approved size:

  * each split gets floor(size / N) quantized down to the venue's size step;
  * the remainder goes to the FIRST splits, one step each, deterministically, so
    the total is exact and two runs of the same input produce the same ladder;
  * if a split would land under the venue minimum, N is REDUCED — smallest N that
    keeps every split valid — rather than emitting orders the venue will reject.

WRITE BEFORE ACT. Every order row is persisted PENDING before the venue is called
(A4). A crash between the write and the call leaves a recoverable record; a crash
without one leaves an order at the venue that ARC has never heard of.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal
from typing import Final

from arc.domain.enums import Direction, MarketPhase, OrderState
from arc.domain.models import ExecutionIntent, Order
from arc.domain.money import dec_str, quantize_size
from arc.errors import ArcError, MarketPhaseError, PostOnlyWouldCrossError
from arc.execution.orders import new_order, transition
from arc.execution.protocol import Executor
from arc.execution.ratelimit import TokenBucket
from arc.execution.retry import (
    IMMEDIATE_RETRY_LIMIT,
    Disposition,
    classify,
    rejection_reason,
)
from arc.logging_setup import log_event
from arc.storage.store import Store

__all__ = ["SubmissionPlan", "Submitter", "split_size"]

_ZERO: Final[Decimal] = Decimal("0")


class SplitError(ArcError):
    """The approved exposure cannot be divided into any valid submission."""


def split_size(
    total: Decimal,
    count: int,
    *,
    minimum: Decimal,
    size_step: Decimal,
) -> tuple[Decimal, ...]:
    """Divide `total` into at most `count` valid order sizes summing to `total`.

    Returns fewer than `count` sizes when the venue minimum forbids that many, and
    an empty tuple when even one order would be under the minimum.

    The remainder is distributed one size-step at a time onto the earliest splits.
    That rule is arbitrary but it must be SOME fixed rule: distributing it onto the
    largest, or the last, or by rounding each split independently, all produce
    ladders that differ between a first run and a post-crash replay, and a replay
    that produces different order sizes produces different order ids and therefore
    duplicate orders at the venue.
    """
    if count < 1:
        raise SplitError(f"submission count must be at least 1, got {count}")
    if total <= _ZERO:
        raise SplitError(f"approved size must be positive, got {total}")

    quantized_total = quantize_size(total, size_step)
    if quantized_total < minimum:
        return ()

    # Largest N whose even share still clears the venue minimum. Reducing rather
    # than emitting an invalid order: an order under the minimum is rejected by the
    # venue, so the window would place N-1 orders and silently under-expose.
    usable = min(count, int(quantized_total / minimum))
    if usable < 1:
        return ()

    base = quantize_size(quantized_total / usable, size_step)
    sizes = [base] * usable
    remainder = quantized_total - base * usable
    index = 0
    while remainder >= size_step:
        sizes[index] += size_step
        remainder -= size_step
        index = (index + 1) % usable
    if remainder > _ZERO:
        # Sub-step dust. It cannot be placed as its own quantity, and adding it to
        # a split would produce an unquantized size the venue rejects, so it is
        # dropped. Dropping under-exposes by less than one share; keeping it would
        # invalidate the whole submission.
        pass
    return tuple(sizes)


@dataclass(frozen=True, slots=True)
class SubmissionPlan:
    """The orders one intent becomes. Built before anything is sent."""

    market_slug: str
    offset_seconds: int
    direction: Direction
    price: Decimal
    orders: tuple[Order, ...]

    @property
    def total_size(self) -> Decimal:
        return sum((o.size for o in self.orders), _ZERO)


class Submitter:
    """Turns one intent into resting passive orders. Idempotent by construction."""

    __slots__ = ("_bucket", "_executor", "_logger", "_minimum", "_size_step", "_store")

    def __init__(
        self,
        store: Store,
        executor: Executor,
        *,
        bucket: TokenBucket,
        minimum: Decimal,
        size_step: Decimal = Decimal("1"),
        logger: logging.Logger | None = None,
    ) -> None:
        self._store = store
        self._executor = executor
        self._bucket = bucket
        self._minimum = minimum
        self._size_step = size_step
        self._logger = logger

    def plan(self, intent: ExecutionIntent, count: int, now: float) -> SubmissionPlan:
        """Build the order ladder. Pure: touches neither the store nor the venue.

        Every value comes from the intent unchanged. The only arithmetic performed
        anywhere in this package on an approved number is the division below.
        """
        sizes = split_size(
            intent.size, count, minimum=self._minimum, size_step=self._size_step
        )
        orders = tuple(
            new_order(
                market_slug=intent.market_slug,
                offset_seconds=intent.offset_seconds,
                index=index,
                generation=0,
                direction=intent.direction,
                price=intent.limit_price,
                size=size,
                now=now,
                trace_id=intent.trace_id,
            )
            for index, size in enumerate(sizes)
        )
        return SubmissionPlan(
            market_slug=intent.market_slug,
            offset_seconds=intent.offset_seconds,
            direction=intent.direction,
            price=intent.limit_price,
            orders=orders,
        )

    async def submit(
        self,
        intent: ExecutionIntent,
        *,
        count: int,
        phase: MarketPhase,
        now: float,
    ) -> tuple[Order, ...]:
        """Place the whole ladder. Returns the orders as they stand afterwards.

        `phase` is the ONE execution boundary (A10/D1). Nothing here compares a
        clock to decide whether the window is too late — that question is not asked
        anywhere in this package.
        """
        if phase is not MarketPhase.ACTIVE:
            raise MarketPhaseError(
                f"{intent.market_slug} is {phase}; submissions are refused outside ACTIVE"
            )

        plan = self.plan(intent, count, now)
        if not plan.orders:
            log_event(
                logging.WARNING,
                "Submission Skipped",
                f"{intent.offset_seconds}s  size {dec_str(intent.size)} below minimum "
                f"{dec_str(self._minimum)}",
                logger=self._logger,
            )
            return ()

        placed: list[Order] = []
        for order in plan.orders:
            placed.append(await self._place_one(order, now))
        log_event(
            logging.INFO,
            "Orders Submitted",
            f"{intent.offset_seconds}s  {intent.direction.value}  "
            f"{dec_str(plan.price)}  {len(placed)}x  {dec_str(plan.total_size)} sh",
            logger=self._logger,
        )
        return tuple(placed)

    async def _place_one(self, order: Order, now: float) -> Order:
        """Persist, then send. Never blind-retries a request of unknown outcome."""
        existing = self._existing(order.order_id)
        if existing is not None:
            # Recomputed ids make replay safe: the same submission after a restart
            # resolves to the row already written rather than to a second order.
            return existing

        self._store.save_order(order)

        attempts = 0
        while True:
            attempts += 1
            await self._bucket.acquire(now)
            try:
                venue_id = await self._executor.place(order)
            except Exception as exc:
                disposition = classify(exc)
                if disposition is Disposition.RETRY_NOW and attempts < IMMEDIATE_RETRY_LIMIT:
                    # No backoff. The venue calls this a rate limit; it is a
                    # transient latency reject, and sleeping through the remaining
                    # milliseconds of a 3-second window loses the window (A14).
                    continue
                if disposition is Disposition.INDETERMINATE:
                    # The request may have reached the venue. Retrying would
                    # double-fill, and both orders would look entirely genuine
                    # afterwards. Reconciliation resolves it against the venue's
                    # own list instead.
                    transition(order, OrderState.INDETERMINATE, now, str(exc))
                    self._store.save_order(order)
                    log_event(
                        logging.WARNING,
                        "Order Unknown",
                        f"{order.order_id}  {exc}",
                        logger=self._logger,
                    )
                    return order
                reason = rejection_reason(exc)
                transition(order, OrderState.REJECTED, now, reason)
                self._store.save_order(order)
                log_event(
                    logging.WARNING,
                    # Its own event name, so the post-only cross is greppable in the
                    # log as the distinct outcome it is rather than as one more
                    # rejection among the venue's other refusals.
                    "Post-Only Would Cross"
                    if isinstance(exc, PostOnlyWouldCrossError)
                    else "Order Rejected",
                    f"{order.order_id}  {exc}",
                    logger=self._logger,
                )
                return order

            order.venue_order_id = venue_id
            transition(order, OrderState.SUBMITTED, now)
            self._store.save_order(order)
            return order

    def _existing(self, order_id: str) -> Order | None:
        for row in self._store.orders_for(order_id.split(":", 1)[0]):
            if row.order_id == order_id:
                return row
        return None
