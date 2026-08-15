"""V2 live adapter. The only file in the engine that talks to the real venue.

Everything above this module is identical in V1 and V2. That is the whole design:
a paper run is evidence about the live run because the same submission code, the
same order FSM, the same fill accounting and the same sweep execute in both.

THE SDK. `polymarket-client` is the official client (A5). `py-clob-client` is
archived and is used nowhere. The official client is natively async and returns
pydantic models whose numeric fields are already `Decimal`, so there is no
worker-thread dispatch here and no float ever touches a price or a size: a float
round-trip does not return the number it was given, and a size that comes back one
ulp low fails the venue's own quantization check.

NO CLIENT ORDER ID. The CLOB assigns order ids and documents no field for the
caller's own identifier. Venue rows are therefore linked back to ARC's derived
order ids through `LocalIdResolver`, which the caller backs with the persisted
`orders.venue_order_id` column — the same mapping after a restart, because it
lives in SQLite and not in this object. Inventing an identifier field the venue
does not document would produce code that looks correct and silently fails to
match.

The one case that mapping cannot cover is a submission whose response was lost:
ARC never learned the venue id, so there is nothing persisted to map. That row
appears at the venue with no local link and reconciliation reports it as an
orphan, which blocks trading rather than guessing. That is the intended outcome —
the alternative is cancelling an id that may not be ARC's on a shared account.
"""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Final, Protocol

import polymarket

from arc.domain.enums import Direction, Mode
from arc.domain.models import Fill, Order
from arc.errors import (
    ArcError,
    CancelAckTimeoutError,
    ConnectionLostError,
    PostOnlyWouldCrossError,
    TransientLatencyRejectError,
)
from arc.execution.protocol import VenueOrder

if TYPE_CHECKING:  # pragma: no cover - import only for annotations
    from polymarket import AsyncSecureClient

__all__ = ["LiveExecutor", "LocalIdResolver", "TokenResolver"]

# The venue's wording for a transient latency reject. Kept alongside the typed
# check because the same condition also arrives as a plain non-success status.
# The caller retries it immediately and without backoff (A14).
_TRANSIENT_MARKER: Final[str] = "global rate limit exceeded"

# The HTTP status carrying that same meaning.
_TOO_MANY_REQUESTS: Final[int] = 429

# A trade the chain refused. Counting it as filled quantity would report a
# position that does not exist.
_DEAD_TRADE_STATUS: Final[str] = "FAILED"

# The venue's reject code for a post-only order that would have taken liquidity.
# Terminal for the submission and never retried: the limit price came from an
# immutable intent, so a retry either re-crosses at the same price or invents a
# price the risk gates never saw.
_POST_ONLY_WOULD_CROSS: Final[str] = "post_only_would_cross"


class TokenResolver(Protocol):
    """Maps a market and a side to the CLOB token id to trade.

    Supplied by the caller from official market metadata (`clobTokenIds`). Not
    derived here: a token id guessed or reordered would place a real order on the
    opposite outcome, and the order would look completely healthy.
    """

    def __call__(self, market_slug: str, direction: Direction) -> str: ...


class LocalIdResolver(Protocol):
    """Maps a venue order id back to ARC's derived order id.

    Backed by the persisted `orders.venue_order_id` column, never by an in-memory
    dict: the process that needs this mapping most is the one that just restarted
    and holds nothing in memory. Returns "" when the venue id is unknown locally,
    which is what makes an orphan visible as an orphan instead of silently
    matching the wrong row.
    """

    def __call__(self, market_slug: str, venue_order_id: str) -> str: ...


def _wrap(exc: Exception) -> Exception:
    """Classify an SDK exception into ARC's taxonomy (A14).

    Typed, not string-matched. The official client raises distinct classes, and a
    classifier keyed on message text reclassifies itself the day the venue rewords
    an error — silently, and in the direction of retrying something that must
    never be retried.
    """
    if isinstance(exc, polymarket.RateLimitError):
        return TransientLatencyRejectError(str(exc))
    if isinstance(exc, polymarket.RequestRejectedError):
        if exc.status == _TOO_MANY_REQUESTS or _TRANSIENT_MARKER in str(exc).lower():
            return TransientLatencyRejectError(str(exc))
        # A non-success status is a definite answer: the request arrived and the
        # venue refused it.
        return ArcError(str(exc))
    if isinstance(
        exc,
        polymarket.ConnectionLostError | polymarket.TransportError | polymarket.TimeoutError,
    ):
        # The request may or may not have reached the venue. Never retried: a
        # retry landing on top of an order that did arrive double-fills, and both
        # orders look entirely genuine afterwards.
        return ConnectionLostError(str(exc))
    if isinstance(exc, polymarket.PolymarketError):
        return ArcError(str(exc))
    return exc


class LiveExecutor:
    """The real CLOB, behind the same interface the paper adapter implements."""

    __slots__ = ("_client", "_local_id", "_logger", "_resolve")

    def __init__(
        self,
        client: AsyncSecureClient,
        resolver: TokenResolver,
        local_ids: LocalIdResolver,
        *,
        logger: logging.Logger | None = None,
    ) -> None:
        self._client = client
        self._resolve = resolver
        self._local_id = local_ids
        self._logger = logger

    @property
    def mode(self) -> Mode:
        return Mode.V2

    # ── the Executor interface ───────────────────────────────────────────────

    async def place(self, order: Order) -> str:
        token_id = self._resolve(order.market_slug, order.direction)
        try:
            # post_only: the whole posture is passive. Without it a marketable
            # price crosses the spread and pays taker fees on an order that was
            # sized as a maker, which is a different trade from the approved one.
            response = await self._client.place_limit_order(
                token_id=token_id,
                price=order.price,
                size=order.size,
                side="BUY",
                post_only=True,
            )
        except Exception as exc:
            raise _wrap(exc) from exc

        if not response.ok:
            if response.code == _POST_ONLY_WOULD_CROSS:
                raise PostOnlyWouldCrossError(
                    f"post-only order {order.order_id} would have crossed at "
                    f"{order.price}: {response.message}"
                )
            raise ArcError(
                f"venue rejected {order.order_id}: {response.code} {response.message}"
            )
        if not response.order_id:
            # The venue accepted something and did not say what. Treated as
            # unknown rather than as success: recording no id would leave an order
            # that can never be cancelled and never be reconciled.
            raise ConnectionLostError(
                f"order {order.order_id} was accepted without an order id: {response!r}"
            )
        return response.order_id

    async def cancel(self, order: Order) -> None:
        if not order.venue_order_id:
            raise CancelAckTimeoutError(
                f"order {order.order_id} has no venue id; it cannot be cancelled and "
                "must be resolved by reconciliation"
            )
        try:
            response = await self._client.cancel_order(order_id=order.venue_order_id)
        except Exception as exc:
            raise _wrap(exc) from exc

        # Compared by value rather than by dict lookup: the SDK keys this map with
        # a NewType over str, and a plain .get() on it does not type-check.
        refusal = next(
            (why for oid, why in response.not_canceled.items() if oid == order.venue_order_id),
            None,
        )
        if refusal is not None:
            raise ArcError(f"venue refused to cancel {order.venue_order_id}: {refusal}")
        if order.venue_order_id not in response.canceled:
            # Neither confirmed nor refused. The order may still be resting, so
            # this is an unacknowledged cancel and the caller marks it
            # INDETERMINATE rather than assuming either outcome.
            raise CancelAckTimeoutError(
                f"cancel of {order.venue_order_id} was not acknowledged: {response!r}"
            )

    async def open_orders(self, market_slug: str) -> tuple[VenueOrder, ...]:
        rows: list[VenueOrder] = []
        for direction in (Direction.UP, Direction.DOWN):
            token_id = self._resolve(market_slug, direction)
            for row in await self._drain(self._client.list_open_orders(token_id=token_id)):
                rows.append(
                    VenueOrder(
                        venue_order_id=row.id,
                        client_order_id=self._local_id(market_slug, row.id),
                        price=row.price,
                        size=row.original_size,
                        filled_size=row.size_matched,
                        resting=True,
                    )
                )
        return tuple(rows)

    async def fills(self, market_slug: str) -> tuple[Fill, ...]:
        """Every fill for this market from the beginning, never a delta.

        ARC posts passively, so its own matched quantity is in the trade's
        `maker_orders` entries and NOT in the trade's top-level size, which is the
        taker's. Reading the top-level figure would over-report every fill by
        whatever the rest of the book contributed to the same trade.
        """
        out: list[Fill] = []
        for direction in (Direction.UP, Direction.DOWN):
            token_id = self._resolve(market_slug, direction)
            for trade in await self._drain(
                self._client.list_account_trades(token_id=token_id)
            ):
                if trade.status == _DEAD_TRADE_STATUS:
                    continue
                out.extend(self._fills_from(market_slug, trade))
        return tuple(out)

    def _fills_from(self, market_slug: str, trade: Any) -> list[Fill]:
        """Split one venue trade into the ARC orders it actually matched."""
        ts = trade.matched_at.timestamp()
        found: list[Fill] = []
        for maker in trade.maker_orders:
            local = self._local_id(market_slug, maker.order_id)
            if not local:
                continue
            found.append(
                Fill(
                    # One trade can match several of ARC's own orders at once, so
                    # the trade id alone is not unique; keyed on it, the second
                    # match would be swallowed by the fill_id primary key and the
                    # position would be understated permanently.
                    fill_id=f"{trade.id}:{maker.order_id}",
                    order_id=local,
                    market_slug=market_slug,
                    size=maker.matched_amount,
                    price=maker.price,
                    ts=ts,
                )
            )
        if found:
            return found

        # A taker fill. post_only should make this unreachable; it is handled
        # anyway because a dropped execution is real money and nothing downstream
        # would ever notice its absence.
        local = self._local_id(market_slug, trade.taker_order_id)
        if not local:
            return []
        return [
            Fill(
                fill_id=trade.id,
                order_id=local,
                market_slug=market_slug,
                size=trade.size,
                price=trade.price,
                ts=ts,
            )
        ]

    async def best_price(self, market_slug: str, direction: Direction) -> Decimal | None:
        token_id = self._resolve(market_slug, direction)
        try:
            book = await self._client.get_order_book(token_id=token_id)
        except Exception as exc:
            raise _wrap(exc) from exc
        if not book.bids:
            return None
        # Taken by max rather than by index so a change in the venue's ordering
        # cannot silently make ARC join the worst price on the book.
        return max(level.price for level in book.bids)

    def mark_price(self, market_slug: str, direction: Direction) -> Decimal | None:
        """Synchronous mark. The live book is only ever reachable over the wire,
        so there is no cached value to read without an await — marking a live
        position from a synchronous payload builder returns None and the card shows
        the figure as unavailable rather than blocking the whole status document on
        one round-trip. The paper executor reads its in-memory book directly."""
        return None

    @staticmethod
    async def _drain(paginator: Any) -> list[Any]:
        """Read a paginator to the end. A partial page is never a whole answer.

        Both callers treat the result as the complete venue-side truth — what is
        resting, what has filled — so a half-drained paginator reads as "there is
        nothing else out there" and reconciliation would close orders that are
        still live.
        """
        try:
            return [item async for item in paginator.iter_items()]
        except Exception as exc:
            raise _wrap(exc) from exc
