"""V2 live adapter. The only file in the engine that talks to the real venue.

Everything above this module is identical in V1 and V2. That is the whole design:
a paper run is evidence about the live run because the same submission code, the
same order FSM, the same fill accounting and the same sweep execute in both.

Two properties of the real venue shape this file.

BLOCKING SDK. py_clob_client is synchronous. Every call is dispatched to a worker
thread, so a slow venue response cannot stall the price feed, the window pass or
the dashboard — all of which share this process's event loop.

NO CLIENT ORDER ID. The CLOB assigns order ids and provides no field for the
caller's own identifier, so a submission whose HTTP response is lost leaves ARC
with no venue id for an order that may well be resting. `client_order_id` is
therefore returned empty here, and reconciliation matches those orders on the
persisted venue id and, failing that, on their (price, size) fingerprint. This is
stated rather than worked around: inventing an identifier field the venue does not
document would produce code that looks correct and silently fails to match.
"""

from __future__ import annotations

import asyncio
import logging
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Final, Protocol

from arc.domain.enums import Direction, Mode
from arc.domain.models import Fill, Order
from arc.domain.money import dec_str, to_decimal
from arc.errors import ArcError, CancelAckTimeoutError, ConnectionLostError
from arc.execution.protocol import VenueOrder

if TYPE_CHECKING:  # pragma: no cover - import only for annotations
    from py_clob_client.client import ClobClient

__all__ = ["LiveExecutor", "TokenResolver"]

# The venue's wording for a transient latency reject. Classified by the caller,
# which retries it immediately and without backoff (A14).
_TRANSIENT_MARKER: Final[str] = "global rate limit exceeded"

# Failures whose outcome is unknown. Matched on the message because the SDK raises
# a single exception type for everything; a connection error means the request may
# or may not have reached the venue, and the caller must never retry it.
_UNKNOWN_MARKERS: Final[tuple[str, ...]] = (
    "timed out",
    "timeout",
    "connection",
    "connectionerror",
    "remote end closed",
    "read timed out",
)


class TokenResolver(Protocol):
    """Maps a market and a side to the CLOB token id to trade.

    Supplied by the caller from official market metadata (`clobTokenIds`). Not
    derived here: a token id guessed or reordered would place a real order on the
    opposite outcome, and the order would look completely healthy.
    """

    def __call__(self, market_slug: str, direction: Direction) -> str: ...


def _wrap(exc: Exception) -> Exception:
    """Classify an SDK exception into ARC's taxonomy."""
    text = str(exc).lower()
    if _TRANSIENT_MARKER in text:
        from arc.errors import TransientLatencyRejectError

        return TransientLatencyRejectError(str(exc))
    if any(marker in text for marker in _UNKNOWN_MARKERS):
        return ConnectionLostError(str(exc))
    return ArcError(str(exc))


class LiveExecutor:
    """The real CLOB, behind the same interface the paper adapter implements."""

    __slots__ = ("_client", "_logger", "_resolve", "_venue_ids")

    def __init__(
        self,
        client: ClobClient,
        resolver: TokenResolver,
        *,
        logger: logging.Logger | None = None,
    ) -> None:
        self._client = client
        self._resolve = resolver
        self._logger = logger
        # venue order id -> the market it belongs to, so open_orders and fills can
        # be scoped without re-querying metadata on every poll.
        self._venue_ids: dict[str, str] = {}

    @property
    def mode(self) -> Mode:
        return Mode.V2

    async def _call(self, fn: Any, *args: Any, **kwargs: Any) -> Any:
        try:
            return await asyncio.to_thread(fn, *args, **kwargs)
        except Exception as exc:
            raise _wrap(exc) from exc

    # ── the Executor interface ───────────────────────────────────────────────

    async def place(self, order: Order) -> str:
        from py_clob_client.clob_types import OrderArgs, OrderType

        token_id = self._resolve(order.market_slug, order.direction)
        args = OrderArgs(
            token_id=token_id,
            price=float(dec_str(order.price)),
            size=float(dec_str(order.size)),
            side="BUY",
        )
        signed = await self._call(self._client.create_order, args)
        # post_only: the whole posture is passive. Without it a marketable price
        # crosses the spread and pays taker fees on an order that was sized as a
        # maker, which is a different trade from the one that was approved.
        response = await self._call(
            self._client.post_order, signed, OrderType.GTC, True
        )
        venue_id = _field(response, "orderID", "orderId", "id")
        if not venue_id:
            # The venue accepted something and did not say what. Treated as unknown
            # rather than as success: recording no id would leave an order that can
            # never be cancelled or reconciled.
            raise ConnectionLostError(
                f"order {order.order_id} was accepted without an order id: {response!r}"
            )
        self._venue_ids[str(venue_id)] = order.market_slug
        return str(venue_id)

    async def cancel(self, order: Order) -> None:
        if not order.venue_order_id:
            raise CancelAckTimeoutError(
                f"order {order.order_id} has no venue id; it cannot be cancelled and "
                "must be resolved by reconciliation"
            )
        response = await self._call(self._client.cancel, order.venue_order_id)
        cancelled = _field(response, "canceled", "cancelled") or ()
        not_cancelled = _field(response, "not_canceled", "not_cancelled") or {}
        if order.venue_order_id in not_cancelled:
            raise ArcError(
                f"venue refused to cancel {order.venue_order_id}: "
                f"{not_cancelled[order.venue_order_id]}"
            )
        if cancelled and order.venue_order_id not in cancelled:
            # Neither confirmed nor refused. The order may still be resting, so this
            # is an unacknowledged cancel and the caller marks it INDETERMINATE.
            raise CancelAckTimeoutError(
                f"cancel of {order.venue_order_id} was not acknowledged: {response!r}"
            )

    async def open_orders(self, market_slug: str) -> tuple[VenueOrder, ...]:
        from py_clob_client.clob_types import OpenOrderParams

        rows: list[VenueOrder] = []
        for direction in (Direction.UP, Direction.DOWN):
            token_id = self._resolve(market_slug, direction)
            response = await self._call(
                self._client.get_orders, OpenOrderParams(asset_id=token_id)
            )
            for row in _rows(response):
                venue_id = str(_field(row, "id", "orderID", "orderId") or "")
                size = to_decimal(str(_field(row, "original_size", "size") or "0"))
                matched = to_decimal(str(_field(row, "size_matched") or "0"))
                rows.append(
                    VenueOrder(
                        venue_order_id=venue_id,
                        client_order_id="",
                        price=to_decimal(str(_field(row, "price") or "0")),
                        size=size,
                        filled_size=matched,
                        resting=True,
                    )
                )
                self._venue_ids[venue_id] = market_slug
        return tuple(rows)

    async def fills(self, market_slug: str) -> tuple[Fill, ...]:
        from py_clob_client.clob_types import TradeParams

        out: list[Fill] = []
        for direction in (Direction.UP, Direction.DOWN):
            token_id = self._resolve(market_slug, direction)
            response = await self._call(
                self._client.get_trades, TradeParams(asset_id=token_id)
            )
            for row in _rows(response):
                trade_id = str(_field(row, "id", "trade_id") or "")
                venue_order = str(_field(row, "maker_order_id", "order_id") or "")
                if not trade_id or not venue_order:
                    continue
                out.append(
                    Fill(
                        fill_id=trade_id,
                        order_id=venue_order,
                        market_slug=market_slug,
                        size=to_decimal(str(_field(row, "size", "matched_amount") or "0")),
                        price=to_decimal(str(_field(row, "price") or "0")),
                        ts=float(_field(row, "match_time", "timestamp") or 0),
                    )
                )
        return tuple(out)

    async def best_price(self, market_slug: str, direction: Direction) -> Decimal | None:
        token_id = self._resolve(market_slug, direction)
        book = await self._call(self._client.get_order_book, token_id)
        bids = getattr(book, "bids", None) or []
        if not bids:
            return None
        # The SDK returns bids ascending; the best bid is the highest price. Taken
        # by max rather than by index so a change in the venue's ordering cannot
        # silently make ARC join the worst price on the book.
        return max(to_decimal(str(b.price)) for b in bids)


def _field(source: Any, *names: str) -> Any:
    """First present attribute or key. The SDK returns dicts and objects both."""
    for name in names:
        if isinstance(source, dict):
            if name in source:
                return source[name]
        elif hasattr(source, name):
            return getattr(source, name)
    return None


def _rows(response: Any) -> list[Any]:
    """Normalise a paged or bare list response into a list of rows."""
    if response is None:
        return []
    if isinstance(response, dict):
        data = response.get("data")
        return list(data) if isinstance(data, list) else []
    if isinstance(response, list):
        return response
    data = getattr(response, "data", None)
    return list(data) if isinstance(data, list) else []
