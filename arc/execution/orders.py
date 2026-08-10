"""Order identity and the order state machine.

Two responsibilities, both of them about surviving a restart.

IDENTITY. Order ids are DERIVED, never generated. A uuid4 would differ on the
retry after a crash, so the venue would receive a second order it considers
distinct and the position would double. Every id here is a pure function of
(market, window, submission index, generation), so the same submission recomputes
the same id forever and the venue's own duplicate-client-id rejection becomes the
last line of defence rather than the only one.

TRANSITIONS. The FSM is explicit and monotonic: a terminal order can never be
revived, and a live order can never skip to a terminal state without passing
through the transition that records why. INDETERMINATE is reachable from any live
state and can leave for any state, because reconciliation is precisely the act of
learning what an unknown order really was.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Final

from arc.domain.enums import (
    DEFAULT_ENGINE,
    LIVE_ORDER_STATES,
    TERMINAL_ORDER_STATES,
    Direction,
    OrderState,
)
from arc.domain.models import Order
from arc.errors import ArcError

__all__ = [
    "DEFAULT_ENGINE",
    "LEGAL_ORDER_TRANSITIONS",
    "OrderTransitionError",
    "apply_fill",
    "chain_id_for",
    "is_terminal",
    "new_order",
    "next_generation_id",
    "order_id_for",
    "transition",
]

_ZERO: Final[Decimal] = Decimal("0")


class OrderTransitionError(ArcError):
    """An illegal order state transition was attempted.

    Raised rather than silently ignored: an attempt to move a FILLED order back to
    SUBMITTED means some path believes the order is still working, and that path
    will keep acting on it.
    """


# PENDING is the pre-submission row (write-before-act, A4). An order that never
# reached the venue can only be REJECTED or EXPIRED; it cannot be CANCELLED,
# because there is nothing at the venue to cancel.
LEGAL_ORDER_TRANSITIONS: Final[dict[OrderState, tuple[OrderState, ...]]] = {
    OrderState.PENDING: (
        OrderState.SUBMITTED,
        OrderState.REJECTED,
        OrderState.EXPIRED,
        OrderState.INDETERMINATE,
    ),
    OrderState.SUBMITTED: (
        OrderState.PARTIAL,
        OrderState.FILLED,
        OrderState.CANCELLED,
        OrderState.EXPIRED,
        OrderState.REJECTED,
        OrderState.INDETERMINATE,
    ),
    OrderState.PARTIAL: (
        OrderState.FILLED,
        OrderState.CANCELLED,
        OrderState.EXPIRED,
        OrderState.INDETERMINATE,
    ),
    # Reconciliation resolves an unknown into whatever the venue actually holds,
    # including back into a live state when the order turns out to still be resting.
    OrderState.INDETERMINATE: (
        OrderState.SUBMITTED,
        OrderState.PARTIAL,
        OrderState.FILLED,
        OrderState.CANCELLED,
        OrderState.EXPIRED,
        OrderState.REJECTED,
    ),
    OrderState.FILLED: (),
    OrderState.CANCELLED: (),
    OrderState.EXPIRED: (),
    OrderState.REJECTED: (),
}


def is_terminal(state: OrderState) -> bool:
    return state in TERMINAL_ORDER_STATES


def _engine_prefix(engine: str) -> str:
    """The identity prefix for an engine. EMPTY for the default engine, always.

    The default engine gets no prefix so every id it has ever derived stays
    byte-identical: the ids are the primary key of the orders table and the reprice
    chains inside it, so a prefix added to them would orphan every historical row
    from the chain it belongs to. A second engine cannot share that silence — two
    engines deriving `slug:45:0:0` would collide on the primary key, and the later
    save would overwrite the earlier engine's order.
    """
    return "" if engine == DEFAULT_ENGINE else f"{engine}:"


def chain_id_for(
    market_slug: str, offset_seconds: int, index: int, engine: str = DEFAULT_ENGINE
) -> str:
    """Stable identity of one submission slot across its whole reprice chain.

    A cancel-then-place reprice produces several venue orders for one logical
    submission. They share this id so filled quantity can be summed across the
    chain rather than per order (hazard H4).

    `engine` is LAST and defaults to the default engine, so every existing call —
    positional or keyword — produces exactly the string it produced before this
    parameter existed.
    """
    return f"{_engine_prefix(engine)}{market_slug}:{offset_seconds}:{index}"


def order_id_for(
    market_slug: str,
    offset_seconds: int,
    index: int,
    generation: int,
    engine: str = DEFAULT_ENGINE,
) -> str:
    """The client order id. Pure function of its inputs, stable across restarts.

    The generation stays the RIGHTMOST component for both engines. `next_generation_id`
    and the repricer's index lookup both read from the right, so prefixing the engine
    on the left leaves the whole reprice chain working untouched.
    """
    return f"{chain_id_for(market_slug, offset_seconds, index, engine)}:{generation}"


def next_generation_id(order: Order) -> str:
    """The id of the replacement order in a reprice chain.

    Derived by incrementing the trailing generation of the existing id, so a crash
    between the cancel and the replacement recomputes the same replacement id.
    """
    chain, _, generation = order.order_id.rpartition(":")
    if not chain or not generation.isdigit():
        raise OrderTransitionError(
            f"order id {order.order_id!r} is not a derived id and cannot be advanced"
        )
    return f"{chain}:{int(generation) + 1}"


def new_order(
    *,
    market_slug: str,
    offset_seconds: int,
    index: int,
    generation: int,
    direction: Direction,
    price: Decimal,
    size: Decimal,
    now: float,
    trace_id: str = "",
    engine: str = DEFAULT_ENGINE,
) -> Order:
    """Build the pre-submission row. State PENDING; nothing has been sent yet.

    `engine` is written onto the row AND into both derived ids, so ownership is
    recorded in two independent places: the column every shared execution operation
    filters on, and the primary key itself. Either alone would be enough to tell the
    two engines apart; both together mean a row cannot be misattributed by a query
    that forgot the filter, because its id already says which engine derived it.
    """
    return Order(
        order_id=order_id_for(market_slug, offset_seconds, index, generation, engine),
        market_slug=market_slug,
        offset_seconds=offset_seconds,
        direction=direction,
        price=price,
        size=size,
        state=OrderState.PENDING,
        created_at=now,
        updated_at=now,
        reprice_chain_id=chain_id_for(market_slug, offset_seconds, index, engine),
        trace_id=trace_id,
        engine=engine,
    )


def transition(order: Order, target: OrderState, now: float, reason: str = "") -> None:
    """Move an order to `target`, or raise and change nothing.

    Mutates only after the legality check, so a refused transition leaves the
    order exactly as it was and the caller's error handling sees consistent state.
    """
    if target is order.state:
        raise OrderTransitionError(
            f"order {order.order_id} is already {target}; a no-op transition means "
            "two paths both believe they own this order"
        )
    if target not in LEGAL_ORDER_TRANSITIONS[order.state]:
        raise OrderTransitionError(
            f"order {order.order_id} cannot move {order.state} -> {target}"
        )
    order.state = target
    order.updated_at = now
    if reason:
        order.rejection_reason = reason


def apply_fill(order: Order, size: Decimal, now: float) -> None:
    """Add filled quantity and move to PARTIAL or FILLED accordingly.

    Clamps the accumulated quantity at the order size. A venue that reports more
    filled than was ordered is reporting something impossible, and letting the
    excess through would make remaining_size negative and the position overstated;
    clamping keeps the local record inside what was actually authorised.
    """
    if size <= _ZERO:
        raise OrderTransitionError(f"fill size must be positive, got {size}")
    if order.state not in LIVE_ORDER_STATES:
        raise OrderTransitionError(
            f"order {order.order_id} is {order.state} and cannot take a fill"
        )
    order.filled_size = min(order.size, order.filled_size + size)
    order.updated_at = now
    target = OrderState.FILLED if order.filled_size >= order.size else OrderState.PARTIAL
    if target is not order.state:
        order.state = target
