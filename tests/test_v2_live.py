"""The V2 live adapter, against a scripted stand-in for the official SDK.

The venue is the only thing substituted anywhere in the execution tests, and the
substitute answers with the SDK's own pydantic models — built by
`model_validate` from the documented payload shapes — so a field that gets
renamed, retyped or dropped upstream fails here rather than in production.

What these tests are actually for: V1 and V2 must differ ONLY in this file. Every
property the paper adapter is trusted for — a venue id comes back, fills link to
the local order they belong to, an unacknowledged cancel is not a cancel, a
failure is classified before it reaches the caller — has to hold identically here,
because the whole paper-as-evidence argument collapses the moment the two adapters
disagree about what they report upward.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from decimal import Decimal
from pathlib import Path
from typing import Any

import polymarket
import pytest
from execution_fixtures import LIMIT_PRICE, WINDOW_TS, make_market, store_at
from polymarket.models import (
    AcceptedOrder,
    CancelOrdersResponse,
    ClobTrade,
    OpenOrder,
    OrderBook,
    RejectedOrder,
)

from arc.domain.enums import Direction, Mode, OrderState
from arc.errors import (
    ArcError,
    CancelAckTimeoutError,
    ConnectionLostError,
    TransientLatencyRejectError,
)
from arc.execution.orders import new_order, transition
from arc.execution.protocol import Executor
from arc.execution.v2_live import LiveExecutor
from arc.storage.store import Store

NOW = float(WINDOW_TS + 297)
OFFSET = 3
CONDITION = "0x" + "a" * 64
UP_TOKEN = "token-up"
DOWN_TOKEN = "token-down"


# ── the scripted venue ───────────────────────────────────────────────────────


def _open_order(order_id: str, *, size: str, matched: str, token: str = UP_TOKEN) -> OpenOrder:
    return OpenOrder.model_validate(
        {
            "id": order_id,
            "market": CONDITION,
            "asset_id": token,
            "owner": "owner",
            "maker_address": "0xmaker",
            "side": "BUY",
            "price": "0.70",
            "original_size": size,
            "size_matched": matched,
            "outcome": "Up",
            "order_type": "GTC",
            "status": "LIVE",
            "created_at": int(NOW),
        }
    )


def _trade(
    trade_id: str,
    makers: tuple[tuple[str, str], ...],
    *,
    status: str = "MATCHED",
    token: str = UP_TOKEN,
    taker_order_id: str = "taker-1",
    size: str = "50",
) -> ClobTrade:
    return ClobTrade.model_validate(
        {
            "id": trade_id,
            "market": CONDITION,
            "asset_id": token,
            "owner": "owner",
            "maker_address": "0xmaker",
            "taker_order_id": taker_order_id,
            "side": "BUY",
            "trader_side": "MAKER",
            "price": "0.70",
            "size": size,
            "outcome": "Up",
            "status": status,
            "fee_rate_bps": "0",
            "bucket_index": 0,
            "transaction_hash": "0xtx",
            "maker_orders": [
                {
                    "order_id": venue_id,
                    "asset_id": token,
                    "maker_address": "0xmaker",
                    "owner": "owner",
                    "side": "BUY",
                    "price": "0.70",
                    "matched_amount": amount,
                    "outcome": "Up",
                    "fee_rate_bps": "0",
                }
                for venue_id, amount in makers
            ],
            "match_time": int(NOW),
            "last_update": int(NOW),
        }
    )


def _book(bids: tuple[str, ...]) -> OrderBook:
    return OrderBook.model_validate(
        {
            "market": CONDITION,
            "asset_id": UP_TOKEN,
            "timestamp": str(int(NOW * 1000)),
            "bids": [{"price": p, "size": "10"} for p in bids],
            "asks": [],
            "min_order_size": "5",
            "tick_size": "0.01",
            "neg_risk": False,
            "hash": "0xhash",
        }
    )


class _Paginator:
    """The SDK's paginator surface, reduced to what the adapter uses.

    `iter_items` is the whole contract; pages are an implementation detail the
    adapter deliberately does not reach into.
    """

    def __init__(self, items: list[Any], *, fail_after: int | None = None) -> None:
        self._items = items
        self._fail_after = fail_after

    def iter_items(self) -> AsyncIterator[Any]:
        async def gen() -> AsyncIterator[Any]:
            for index, item in enumerate(self._items):
                if self._fail_after is not None and index == self._fail_after:
                    raise polymarket.TransportError("stream died mid-page")
                yield item

        return gen()


class FakeVenue:
    """A scripted CLOB. Records what was sent; answers with real SDK models."""

    def __init__(self) -> None:
        self.placed: list[dict[str, Any]] = []
        self.cancelled: list[str] = []
        self.place_result: Any = AcceptedOrder(
            order_id="venue-1",
            status="live",
            making_amount=Decimal("24.5"),
            taking_amount=Decimal("35"),
            trade_ids=(),
            transactions_hashes=(),
        )
        self.place_error: Exception | None = None
        self.cancel_result = CancelOrdersResponse(canceled=("venue-1",), not_canceled={})
        self.cancel_error: Exception | None = None
        self.open_by_token: dict[str, list[OpenOrder]] = {}
        self.trades_by_token: dict[str, list[ClobTrade]] = {}
        self.book: OrderBook | None = None
        self.book_error: Exception | None = None
        self.pagination_fail_after: int | None = None

    async def place_limit_order(self, **kwargs: Any) -> Any:
        self.placed.append(kwargs)
        if self.place_error is not None:
            raise self.place_error
        return self.place_result

    async def cancel_order(self, *, order_id: str) -> CancelOrdersResponse:
        self.cancelled.append(order_id)
        if self.cancel_error is not None:
            raise self.cancel_error
        return self.cancel_result

    def list_open_orders(self, *, token_id: str) -> _Paginator:
        return _Paginator(
            list(self.open_by_token.get(token_id, ())),
            fail_after=self.pagination_fail_after,
        )

    def list_account_trades(self, *, token_id: str) -> _Paginator:
        return _Paginator(
            list(self.trades_by_token.get(token_id, ())),
            fail_after=self.pagination_fail_after,
        )

    async def get_order_book(self, *, token_id: str) -> OrderBook:
        if self.book_error is not None:
            raise self.book_error
        assert self.book is not None
        return self.book


def _tokens(market_slug: str, direction: Direction) -> str:
    return UP_TOKEN if direction is Direction.UP else DOWN_TOKEN


@pytest.fixture
def wired(tmp_path: Path):  # type: ignore[no-untyped-def]
    """A real Store and a scripted venue behind the real adapter."""
    store = store_at(tmp_path)
    market = make_market(store)
    venue = FakeVenue()
    executor = LiveExecutor(venue, _tokens, store.local_order_id)  # type: ignore[arg-type]
    yield store, venue, executor, market
    store.close()


def _order(market_slug: str, index: int = 0, size: str = "35"):  # type: ignore[no-untyped-def]
    return new_order(
        market_slug=market_slug,
        offset_seconds=OFFSET,
        index=index,
        generation=0,
        direction=Direction.UP,
        price=LIMIT_PRICE,
        size=Decimal(size),
        now=NOW,
    )


def _link(store: Store, order: Any, venue_id: str) -> Any:
    """Persist an order already acknowledged by the venue."""
    order.venue_order_id = venue_id
    transition(order, OrderState.SUBMITTED, NOW)
    store.save_order(order)
    return order


# ── the tests ────────────────────────────────────────────────────────────────


class TestItIsTheSameInterfaceAsPaper:
    def test_it_satisfies_the_executor_protocol(self, wired) -> None:  # type: ignore[no-untyped-def]
        _store, _venue, executor, _market = wired
        assert isinstance(executor, Executor)

    def test_it_reports_v2(self, wired) -> None:  # type: ignore[no-untyped-def]
        _store, _venue, executor, _market = wired
        assert executor.mode is Mode.V2

    def test_the_archived_sdk_is_imported_nowhere(self) -> None:
        """A5: py-clob-client is archived and non-functional."""
        source = Path(__file__).resolve().parents[1] / "arc"
        offenders = [
            str(p)
            for p in source.rglob("*.py")
            if "py_clob_client" in p.read_text(encoding="utf-8")
        ]
        assert not offenders


class TestPlacement:
    def test_it_posts_passively_at_the_intents_price_and_size(self, wired) -> None:  # type: ignore[no-untyped-def]
        """post_only, or a marketable price crosses and pays taker fees on an
        order that was sized as a maker — a different trade from the approved one."""
        _store, venue, executor, market = wired
        asyncio.run(executor.place(_order(market.slug)))

        sent = venue.placed[0]
        assert sent["token_id"] == UP_TOKEN
        assert sent["side"] == "BUY"
        assert sent["post_only"] is True
        assert sent["price"] == LIMIT_PRICE
        assert sent["size"] == Decimal("35")

    def test_no_price_or_size_is_ever_converted_to_float(self, wired) -> None:  # type: ignore[no-untyped-def]
        """A float round-trip of 0.70 is not 0.70, and the venue quantizes."""
        _store, venue, executor, market = wired
        asyncio.run(executor.place(_order(market.slug)))

        sent = venue.placed[0]
        assert isinstance(sent["price"], Decimal)
        assert isinstance(sent["size"], Decimal)

    def test_the_down_side_resolves_to_the_other_token(self, wired) -> None:  # type: ignore[no-untyped-def]
        """A reordered token id places a real order on the opposite outcome."""
        _store, venue, executor, market = wired
        order = _order(market.slug)
        order.direction = Direction.DOWN
        asyncio.run(executor.place(order))
        assert venue.placed[0]["token_id"] == DOWN_TOKEN

    def test_the_venue_order_id_is_returned(self, wired) -> None:  # type: ignore[no-untyped-def]
        _store, _venue, executor, market = wired
        assert asyncio.run(executor.place(_order(market.slug))) == "venue-1"

    def test_a_rejection_is_a_definite_failure(self, wired) -> None:  # type: ignore[no-untyped-def]
        """Definite, not unknown: the venue answered, so no order is resting."""
        _store, venue, executor, market = wired
        venue.place_result = RejectedOrder(code="not_enough_balance", message="no funds")

        with pytest.raises(ArcError) as caught:
            asyncio.run(executor.place(_order(market.slug)))
        assert not isinstance(caught.value, ConnectionLostError)
        assert "not_enough_balance" in str(caught.value)

    def test_a_post_only_cross_is_a_definite_failure_too(self, wired) -> None:  # type: ignore[no-untyped-def]
        """The passive posture refusing to become an aggressive one. Not a retry:
        the price would have to change, and prices are frozen upstream."""
        _store, venue, executor, market = wired
        venue.place_result = RejectedOrder(
            code="post_only_would_cross", message="would cross the book"
        )
        with pytest.raises(ArcError):
            asyncio.run(executor.place(_order(market.slug)))

    def test_an_acceptance_without_an_id_is_unknown_not_success(self, wired) -> None:  # type: ignore[no-untyped-def]
        """Recording no id leaves an order that can never be cancelled."""
        _store, venue, executor, market = wired
        venue.place_result = AcceptedOrder(
            order_id="",
            status="live",
            making_amount=Decimal("0"),
            taking_amount=Decimal("0"),
            trade_ids=(),
            transactions_hashes=(),
        )
        with pytest.raises(ConnectionLostError):
            asyncio.run(executor.place(_order(market.slug)))


class TestFailureClassification:
    """A14, keyed on the SDK's own exception types rather than on message text."""

    @pytest.mark.parametrize(
        ("raised", "expected"),
        [
            (polymarket.RateLimitError("Global Rate Limit Exceeded"), TransientLatencyRejectError),
            (
                polymarket.ConnectionLostError("socket closed", code=1006, reason=""),
                ConnectionLostError,
            ),
            (polymarket.TransportError("read timed out"), ConnectionLostError),
            (polymarket.TimeoutError("no response"), ConnectionLostError),
            (polymarket.UnexpectedResponseError("garbage"), ArcError),
        ],
    )
    def test_each_sdk_error_maps_to_its_arc_error(
        self, wired, raised: Exception, expected: type[Exception]
    ) -> None:  # type: ignore[no-untyped-def]
        _store, venue, executor, market = wired
        venue.place_error = raised
        with pytest.raises(expected):
            asyncio.run(executor.place(_order(market.slug)))

    def test_a_429_is_transient_and_retried_immediately(self, wired) -> None:  # type: ignore[no-untyped-def]
        """Not a backoff: sleeping spends the remaining milliseconds of the window."""
        _store, venue, executor, market = wired
        venue.place_error = polymarket.RequestRejectedError("slow down", status=429)
        with pytest.raises(TransientLatencyRejectError):
            asyncio.run(executor.place(_order(market.slug)))

    def test_a_400_is_a_definite_refusal_not_an_unknown(self, wired) -> None:  # type: ignore[no-untyped-def]
        """The request demonstrably arrived, so nothing is resting to reconcile."""
        _store, venue, executor, market = wired
        venue.place_error = polymarket.RequestRejectedError("bad order", status=400)
        with pytest.raises(ArcError) as caught:
            asyncio.run(executor.place(_order(market.slug)))
        assert not isinstance(caught.value, ConnectionLostError | TransientLatencyRejectError)

    def test_a_connection_loss_is_never_reclassified_as_a_failure(self, wired) -> None:  # type: ignore[no-untyped-def]
        """FAIL would mark an order dead that may be resting; the retry double-fills."""
        from arc.execution.retry import Disposition, classify

        _store, venue, executor, market = wired
        venue.place_error = polymarket.ConnectionLostError(
            "connection reset", code=1006, reason=""
        )
        with pytest.raises(ConnectionLostError) as caught:
            asyncio.run(executor.place(_order(market.slug)))
        assert classify(caught.value) is Disposition.INDETERMINATE


class TestCancellation:
    def test_a_confirmed_cancel_returns_quietly(self, wired) -> None:  # type: ignore[no-untyped-def]
        store, venue, executor, market = wired
        order = _link(store, _order(market.slug), "venue-1")
        asyncio.run(executor.cancel(order))
        assert venue.cancelled == ["venue-1"]

    def test_an_order_with_no_venue_id_cannot_be_cancelled(self, wired) -> None:  # type: ignore[no-untyped-def]
        """It goes to reconciliation, not to a guessed id."""
        _store, venue, executor, market = wired
        with pytest.raises(CancelAckTimeoutError):
            asyncio.run(executor.cancel(_order(market.slug)))
        assert venue.cancelled == []

    def test_a_refused_cancel_raises_with_the_venues_reason(self, wired) -> None:  # type: ignore[no-untyped-def]
        store, venue, executor, market = wired
        order = _link(store, _order(market.slug), "venue-1")
        venue.cancel_result = CancelOrdersResponse(
            canceled=(), not_canceled={"venue-1": "order not found"}
        )
        with pytest.raises(ArcError, match="order not found"):
            asyncio.run(executor.cancel(order))

    def test_an_unacknowledged_cancel_is_not_a_cancel(self, wired) -> None:  # type: ignore[no-untyped-def]
        """Silence is not consent: the order may still be resting, so the caller
        marks it INDETERMINATE and the sweep keeps counting it as live (A13)."""
        store, venue, executor, market = wired
        order = _link(store, _order(market.slug), "venue-1")
        venue.cancel_result = CancelOrdersResponse(canceled=("someone-else",), not_canceled={})
        with pytest.raises(CancelAckTimeoutError):
            asyncio.run(executor.cancel(order))


class TestOpenOrders:
    def test_a_resting_order_is_linked_back_to_its_local_id(self, wired) -> None:  # type: ignore[no-untyped-def]
        """The mapping that makes reconciliation work at all."""
        store, venue, executor, market = wired
        local = _link(store, _order(market.slug), "venue-1")
        venue.open_by_token[UP_TOKEN] = [_open_order("venue-1", size="35", matched="10")]

        rows = asyncio.run(executor.open_orders(market.slug))

        assert len(rows) == 1
        assert rows[0].venue_order_id == "venue-1"
        assert rows[0].client_order_id == local.order_id
        assert rows[0].size == Decimal("35")
        assert rows[0].filled_size == Decimal("10")
        assert rows[0].resting

    def test_the_link_survives_a_restart(self, wired, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
        """It is read from SQLite, not from a dict this process happens to hold —
        the process that needs the mapping most is the one that just restarted."""
        store, venue, _executor, market = wired
        local = _link(store, _order(market.slug), "venue-1")
        store.close()

        reopened = store_at(tmp_path)
        fresh = LiveExecutor(venue, _tokens, reopened.local_order_id)  # type: ignore[arg-type]
        venue.open_by_token[UP_TOKEN] = [_open_order("venue-1", size="35", matched="0")]

        rows = asyncio.run(fresh.open_orders(market.slug))
        assert rows[0].client_order_id == local.order_id
        reopened.close()

    def test_an_unknown_venue_order_reports_no_local_id(self, wired) -> None:  # type: ignore[no-untyped-def]
        """That empty string is what reconciliation reads as an orphan; inventing a
        match would let ARC cancel an order that may not be its own."""
        _store, venue, executor, market = wired
        venue.open_by_token[UP_TOKEN] = [_open_order("stranger", size="5", matched="0")]

        rows = asyncio.run(executor.open_orders(market.slug))
        assert rows[0].client_order_id == ""

    def test_both_sides_of_the_book_are_queried(self, wired) -> None:  # type: ignore[no-untyped-def]
        """A DOWN order missed here reads as "nothing resting" and gets abandoned."""
        _store, venue, executor, market = wired
        venue.open_by_token[UP_TOKEN] = [_open_order("v-up", size="5", matched="0")]
        venue.open_by_token[DOWN_TOKEN] = [
            _open_order("v-down", size="5", matched="0", token=DOWN_TOKEN)
        ]

        rows = asyncio.run(executor.open_orders(market.slug))
        assert {r.venue_order_id for r in rows} == {"v-up", "v-down"}

    def test_a_broken_page_raises_rather_than_reporting_an_empty_book(self, wired) -> None:  # type: ignore[no-untyped-def]
        """A half-drained paginator read as "nothing else is out there" would let
        reconciliation close orders that are still live."""
        _store, venue, executor, market = wired
        venue.open_by_token[UP_TOKEN] = [
            _open_order("v-1", size="5", matched="0"),
            _open_order("v-2", size="5", matched="0"),
        ]
        venue.pagination_fail_after = 1

        with pytest.raises(ConnectionLostError):
            asyncio.run(executor.open_orders(market.slug))


class TestFills:
    def test_a_maker_fill_is_attributed_to_the_local_order(self, wired) -> None:  # type: ignore[no-untyped-def]
        """The V1/V2 parity property. Keyed on the venue id, every fill would land
        in the unlinked branch and no order would ever advance."""
        store, venue, executor, market = wired
        local = _link(store, _order(market.slug), "venue-1")
        venue.trades_by_token[UP_TOKEN] = [_trade("t-1", (("venue-1", "12"),))]

        fills = asyncio.run(executor.fills(market.slug))

        assert len(fills) == 1
        assert fills[0].order_id == local.order_id
        assert fills[0].size == Decimal("12")
        assert fills[0].price == Decimal("0.70")
        assert fills[0].ts == NOW

    def test_the_makers_matched_amount_is_used_not_the_trades_size(self, wired) -> None:  # type: ignore[no-untyped-def]
        """The trade's own size is the TAKER's. Reading it over-reports every fill
        by whatever the rest of the book contributed to the same trade."""
        store, venue, executor, market = wired
        _link(store, _order(market.slug), "venue-1")
        venue.trades_by_token[UP_TOKEN] = [_trade("t-1", (("venue-1", "12"),), size="500")]

        assert asyncio.run(executor.fills(market.slug))[0].size == Decimal("12")

    def test_one_trade_matching_two_of_our_orders_yields_two_distinct_fills(
        self, wired
    ) -> None:  # type: ignore[no-untyped-def]
        """Keyed on the trade id alone, the second would be swallowed by the
        fill_id primary key and the position understated permanently."""
        store, venue, executor, market = wired
        first = _link(store, _order(market.slug, index=0, size="18"), "venue-1")
        second = _link(store, _order(market.slug, index=1, size="17"), "venue-2")
        venue.trades_by_token[UP_TOKEN] = [
            _trade("t-1", (("venue-1", "18"), ("venue-2", "17")))
        ]

        fills = asyncio.run(executor.fills(market.slug))

        assert len({f.fill_id for f in fills}) == 2
        assert {f.order_id for f in fills} == {first.order_id, second.order_id}
        assert sum(f.size for f in fills) == Decimal("35")

    def test_a_fill_id_is_stable_across_repolls(self, wired) -> None:  # type: ignore[no-untyped-def]
        """Idempotence is arbitrated by that id in SQLite; an unstable one doubles
        the recorded position on the very next poll."""
        store, venue, executor, market = wired
        _link(store, _order(market.slug), "venue-1")
        venue.trades_by_token[UP_TOKEN] = [_trade("t-1", (("venue-1", "12"),))]

        first = asyncio.run(executor.fills(market.slug))
        second = asyncio.run(executor.fills(market.slug))
        assert [f.fill_id for f in first] == [f.fill_id for f in second]

    def test_a_failed_trade_is_not_a_fill(self, wired) -> None:  # type: ignore[no-untyped-def]
        """The chain refused it. Counting it reports a position ARC does not hold."""
        store, venue, executor, market = wired
        _link(store, _order(market.slug), "venue-1")
        venue.trades_by_token[UP_TOKEN] = [
            _trade("t-1", (("venue-1", "12"),), status="FAILED")
        ]
        assert asyncio.run(executor.fills(market.slug)) == ()

    def test_someone_elses_trade_on_the_same_market_is_ignored(self, wired) -> None:  # type: ignore[no-untyped-def]
        _store, venue, executor, market = wired
        venue.trades_by_token[UP_TOKEN] = [_trade("t-1", (("stranger", "12"),))]
        assert asyncio.run(executor.fills(market.slug)) == ()

    def test_a_taker_fill_is_still_recorded(self, wired) -> None:  # type: ignore[no-untyped-def]
        """post_only should make it unreachable, but a dropped execution is real
        money and nothing downstream would notice its absence."""
        store, venue, executor, market = wired
        local = _link(store, _order(market.slug), "venue-1")
        venue.trades_by_token[UP_TOKEN] = [
            _trade("t-1", (), taker_order_id="venue-1", size="9")
        ]

        fills = asyncio.run(executor.fills(market.slug))
        assert [(f.order_id, f.size) for f in fills] == [(local.order_id, Decimal("9"))]

    def test_the_whole_history_is_returned_not_a_delta(self, wired) -> None:  # type: ignore[no-untyped-def]
        """A cursor lost in a crash loses the fills that arrived while ARC was down."""
        store, venue, executor, market = wired
        _link(store, _order(market.slug), "venue-1")
        venue.trades_by_token[UP_TOKEN] = [
            _trade("t-1", (("venue-1", "5"),)),
            _trade("t-2", (("venue-1", "7"),)),
        ]

        asyncio.run(executor.fills(market.slug))
        assert len(asyncio.run(executor.fills(market.slug))) == 2

    def test_a_broken_page_raises_rather_than_losing_fills(self, wired) -> None:  # type: ignore[no-untyped-def]
        store, venue, executor, market = wired
        _link(store, _order(market.slug), "venue-1")
        venue.trades_by_token[UP_TOKEN] = [
            _trade("t-1", (("venue-1", "5"),)),
            _trade("t-2", (("venue-1", "7"),)),
        ]
        venue.pagination_fail_after = 1

        with pytest.raises(ConnectionLostError):
            asyncio.run(executor.fills(market.slug))


class TestBestPrice:
    def test_the_best_bid_is_the_highest_not_the_first(self, wired) -> None:  # type: ignore[no-untyped-def]
        """By max, so a change in the venue's ordering cannot silently make ARC
        join the worst price on the book."""
        _store, venue, executor, market = wired
        venue.book = _book(("0.61", "0.69", "0.64"))
        assert asyncio.run(executor.best_price(market.slug, Direction.UP)) == Decimal("0.69")

    def test_an_empty_book_is_none_not_zero(self, wired) -> None:  # type: ignore[no-untyped-def]
        _store, venue, executor, market = wired
        venue.book = _book(())
        assert asyncio.run(executor.best_price(market.slug, Direction.UP)) is None

    def test_a_book_failure_is_classified(self, wired) -> None:  # type: ignore[no-untyped-def]
        _store, venue, executor, market = wired
        venue.book_error = polymarket.TransportError("no route")
        with pytest.raises(ConnectionLostError):
            asyncio.run(executor.best_price(market.slug, Direction.UP))
