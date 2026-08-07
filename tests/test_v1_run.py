"""V1 against the real pipeline: one market, end to end, to a simulated maker order.

WHAT THIS PINS. V1 and V2 must differ ONLY in the execution adapter. Everything
above it — discovery, PTB, the signal TWAP, the Window Engine, the Decision Engine,
the Risk Engine and the Limit Order Engine — must receive the same inputs in both
modes, and that includes the official CLOB book. A paper run that sized against a
book the live run never saw would not be evidence about the live run at all.

THE BUG THIS WAS WRITTEN FOR. `ArcRuntime._quote` used to resolve the price through
a helper that drove `Executor.best_price` one step and returned None the moment the
coroutine suspended. `PaperExecutor`'s book was written by nothing in the runtime, so
V1's quote was permanently absent and every window skipped with NO_QUOTE; and
`LiveExecutor.best_price` is a real HTTP call, so V2's quote was permanently absent
too. The book is now read once per pass by the runtime and handed to whichever
adapter is running. These tests fail if that ownership moves back into an adapter.

The venue is scripted — an `AsyncPublicClient` stand-in answering with the SDK's own
`OrderBook` and `Market` models, built by `model_validate` from the documented
payload shapes, so a renamed or retyped field fails here rather than in production.
Nothing else is substituted: the real Store, Rotator, WindowEngine, DecisionEngine,
RiskEngine, Submitter and PaperExecutor all run.
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
from decimal import Decimal
from typing import Any

import pytest
from conftest import VALID_TRADING_VALUES
from polymarket.models import Market, OrderBook

from arc.clock import FrozenClock
from arc.config import ArcSettings, Settings, build_trading_config
from arc.decision.reasons import SkipReason
from arc.domain.enums import Direction, MarketPhase, Mode, SettlementSpecStatus
from arc.execution.v1_paper import PaperExecutor
from arc.execution.v2_live import LiveExecutor
from arc.market.discovery import MarketMetadata
from arc.market.feed import RtdsFeed
from arc.runtime.engine import (
    _BOOK_MAX_AGE_SECONDS,
    _BOOK_REFRESH_SECONDS,
    _WALLET_REFRESH_SECONDS,
    ArcRuntime,
    RuntimeStatus,
    TokenCache,
)
from arc.runtime.state import RuntimeState
from arc.storage.store import Store

START_TS = 1754400000
CLOSE_TS = START_TS + 300
CONDITION = "0x" + "a" * 64
UP_TOKEN = "token-up"
DOWN_TOKEN = "token-down"

# The venue publishes a settled market's finalPrice ~25s after it closes.
PUBLICATION_DELAY_SECONDS = 25.0

PTB = Decimal("64000")
BEST_BID = Decimal("0.70")


# ── the scripted official sources ────────────────────────────────────────────


def _book(*bids: str, token: str = UP_TOKEN) -> OrderBook:
    """A real OrderBook, from the documented CLOB payload shape."""
    return OrderBook.model_validate(
        {
            "market": CONDITION,
            "asset_id": token,
            "timestamp": str(START_TS * 1000),
            "bids": [{"price": p, "size": "500"} for p in bids],
            "asks": [],
            "min_order_size": "5",
            "tick_size": "0.01",
            "neg_risk": False,
            "hash": "0xhash",
        }
    )


def _market_metadata(slug: str) -> Market:
    """A real Market, from the documented Gamma payload shape."""
    return Market.model_validate(
        {
            "id": slug,
            "slug": slug,
            "conditionId": CONDITION,
            "outcomes": '["Up", "Down"]',
            "outcomePrices": '["0.5", "0.5"]',
            "clobTokenIds": f'["{UP_TOKEN}", "{DOWN_TOKEN}"]',
            "active": True,
            "closed": False,
        }
    )


class FakeBook:
    """The official public client, reduced to the two calls the runtime makes.

    Counts reads, so a test can prove the runtime — and not the adapter — is the
    one talking to the venue, and that it does so once per market side per refresh
    rather than once per decision.
    """

    def __init__(self, *, bids: tuple[str, ...] = ("0.68", "0.70", "0.69")) -> None:
        self.bids = bids
        self.book_reads: list[str] = []
        self.market_reads: list[str] = []
        self.error: Exception | None = None
        self.closed = False

    async def get_order_book(self, *, token_id: str) -> OrderBook:
        self.book_reads.append(token_id)
        if self.error is not None:
            raise self.error
        return _book(*self.bids, token=token_id)

    async def get_market(self, *, slug: str) -> Market:
        self.market_reads.append(slug)
        return _market_metadata(slug)

    async def close(self) -> None:
        self.closed = True


class FakeDiscovery:
    """Metadata on the venue's real publication schedule. Holds no network."""

    def __init__(self, clock: FrozenClock) -> None:
        self._clock = clock
        self.fetched: list[str] = []

    async def fetch_metadata(self, slug: str) -> MarketMetadata:
        self.fetched.append(slug)
        window_ts = int(slug.rsplit("-", 1)[1])
        close_ts = window_ts + 300
        published = self._clock.now() >= close_ts + PUBLICATION_DELAY_SECONDS
        return MarketMetadata(
            slug=slug,
            condition_id=CONDITION,
            token_ids=(UP_TOKEN, DOWN_TOKEN),
            venue_close_ts=close_ts,
            ptb_raw=None,
            final_price_raw=str(PTB) if published else None,
            active=True,
            closed=published,
            raw={},
        )

    async def aclose(self) -> None:
        return None


# ── the runtime under test ───────────────────────────────────────────────────


def _settings(mode: Mode = Mode.V1) -> Settings:
    return Settings(
        env=ArcSettings(_env_file=None),
        trading=build_trading_config(dict(VALID_TRADING_VALUES)),
        seeded_from_env=False,
    ).with_mode(mode)


def _runtime(
    store: Store,
    clock: FrozenClock,
    *,
    book: FakeBook | None,
    executor: Any = None,
) -> ArcRuntime:
    state = RuntimeState(store, clock)
    state.load()
    run = ArcRuntime(
        settings=_settings(),
        store=store,
        clock=clock,
        runtime=state,
        discovery=FakeDiscovery(clock),  # type: ignore[arg-type]
        feed=RtdsFeed(clock),
        executor=executor if executor is not None else PaperExecutor(),
        out=io.StringIO(),
        book_client=book,  # type: ignore[arg-type]
        logger=logging.getLogger("arc.test.v1run"),
    )
    # Both gates open. Trading is disabled by default and the operator gate disarms
    # on every startup, so a test that wants a submission has to say so — which is
    # the point of A8 and is stated here rather than defaulted.
    state.record_spec_status(SettlementSpecStatus.VERIFIED)
    state.enable_trading()
    run.arm()
    # `run()` sets this; the tests drive the loop body directly, and the risk gates
    # read it as process health.
    run.status = RuntimeStatus.running_for(run.mode)
    return run


def _observe(run: ArcRuntime, clock: FrozenClock, price: Decimal) -> None:
    """One accepted RTDS frame, in the live payload shape."""
    run._handle_frame(
        json.dumps(
            {
                "symbol": "btc/usd",
                "timestamp": clock.now(),
                "value": float(price),
                "full_accuracy_value": str(int(price * 10**18)),
            }
        )
    )


def _pass(run: ArcRuntime, clock: FrozenClock) -> None:
    """One iteration of the real main loop, without its sleep.

    The same calls in the same order as `_main_loop`. Driving them directly keeps
    the test on wall-clock control — the loop never terminates and its cadence is
    not what is under test.
    """
    now = clock.now()
    event = run.rotator.advance(now)
    if event.opened:
        run.stats.markets_processed += 1
        run._next_ptb_attempt = 0.0
        asyncio.run(run._load_tokens(event.opened))
    if event.closed:
        asyncio.run(run._sweeper.sweep(event.closed, now))
    if event.archived:
        run.tokens.drop(event.archived)
        run._forget_book(event.archived)
    asyncio.run(run._attempt_ptb(now))
    asyncio.run(run._refresh_books(now))
    asyncio.run(run._refresh_wallet(now))
    run._watchdog.evaluate()
    run._gate_on_health()
    asyncio.run(run._drive_execution(now))


def _drive_to_intent(run: ArcRuntime, clock: FrozenClock) -> None:
    """Open the market, freeze the PTB, freeze the 15s window, and fire it.

    The prices are chosen so the frozen opening TWAP sits above the official PTB
    (direction UP, trigger = opening + the 15s buffer of 2.00) and the cumulative
    mean then crosses that trigger. Nothing here reaches into a window: every value
    is produced by the real freeze and the real evaluation.
    """
    # An accepted observation before the first pass. The staleness watchdog blocks
    # on "no data yet", and `_gate_on_health` disables trading the moment it does;
    # nothing re-enables it, because re-enabling is the spec check's job. A run that
    # ticked the watchdog only after its first pass would be denied for the rest of
    # its life and would prove nothing about the book.
    _observe(run, clock, Decimal("64100"))
    _pass(run, clock)  # market opens; token ids load

    # Past the venue's publication instant, so the official PTB can be frozen.
    for _ in range(30):
        clock.advance(1.0)
        _observe(run, clock, Decimal("64100"))
        _pass(run, clock)

    # On to the 15s window's activation instant, one second at a time and observing
    # the whole way. Jumping the clock instead would starve the feed, and the
    # staleness watchdog would disable trading before the window ever fired — which
    # is correct behaviour, and would make the run prove nothing about the book.
    while clock.now() < CLOSE_TS - 15:
        clock.advance(1.0)
        _observe(run, clock, Decimal("64100"))
        _pass(run, clock)

    # Now push the cumulative mean past the locked trigger. Stopped short of the
    # 10s window's activation, so exactly one window fires and the assertions below
    # are about a single, identifiable order.
    for _ in range(8):
        clock.advance(0.5)
        _observe(run, clock, Decimal("65000"))
        _pass(run, clock)


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock(now=float(START_TS))


@pytest.fixture
def book() -> FakeBook:
    return FakeBook()


# ── the tests ────────────────────────────────────────────────────────────────


class TestV1SubmitsAgainstTheLiveBook:
    """The end-to-end claim: a V1 runtime places a simulated passive maker order
    priced from the official CLOB, with every other subsystem running for real."""

    def test_the_official_ptb_is_frozen(self, store: Store, clock: FrozenClock, book: FakeBook) -> None:
        run = _runtime(store, clock, book=book)
        _drive_to_intent(run, clock)
        market = run.rotator.current
        assert market is not None
        assert market.ptb == PTB

    def test_the_quote_comes_from_the_official_book(
        self, store: Store, clock: FrozenClock, book: FakeBook
    ) -> None:
        run = _runtime(store, clock, book=book)
        _drive_to_intent(run, clock)
        assert run._quote(run.rotator.current.slug, Direction.UP) == BEST_BID  # type: ignore[union-attr]

    def test_the_best_bid_is_taken_by_price_not_by_position(
        self, store: Store, clock: FrozenClock
    ) -> None:
        """The book is returned worst-first here. Taking bids[0] would make ARC join
        the worst price on the book and nothing about the order would look wrong."""
        run = _runtime(store, clock, book=FakeBook(bids=("0.61", "0.70", "0.64")))
        _drive_to_intent(run, clock)
        assert run._quote(run.rotator.current.slug, Direction.UP) == Decimal("0.70")  # type: ignore[union-attr]

    def test_a_window_freezes_and_fires(self, store: Store, clock: FrozenClock, book: FakeBook) -> None:
        run = _runtime(store, clock, book=book)
        _drive_to_intent(run, clock)
        market = run.rotator.current
        assert market is not None
        window = market.window(15)
        assert window.locked_trigger is not None
        assert window.direction is Direction.UP
        assert window.fired_at is not None

    def test_an_intent_is_created_and_persisted(
        self, store: Store, clock: FrozenClock, book: FakeBook
    ) -> None:
        run = _runtime(store, clock, book=book)
        _drive_to_intent(run, clock)
        slug = run.rotator.current.slug  # type: ignore[union-attr]
        (intent,) = store.intents_for(slug)
        assert intent.offset_seconds == 15
        assert intent.direction is Direction.UP
        assert intent.limit_price == BEST_BID

    def test_a_simulated_passive_maker_order_reaches_the_venue(
        self, store: Store, clock: FrozenClock, book: FakeBook
    ) -> None:
        """The whole point. Persisted first (A4), then resting on the paper book."""
        run = _runtime(store, clock, book=book)
        _drive_to_intent(run, clock)
        slug = run.rotator.current.slug  # type: ignore[union-attr]

        (order,) = store.orders_for(slug)
        assert order.price == BEST_BID
        assert order.size > Decimal("0")

        resting = asyncio.run(run.executor.open_orders(slug))
        assert [r.price for r in resting] == [BEST_BID]

    def test_the_mode_really_is_v1(self, store: Store, clock: FrozenClock, book: FakeBook) -> None:
        run = _runtime(store, clock, book=book)
        assert run.mode is Mode.V1
        assert isinstance(run.executor, PaperExecutor)

    def test_without_a_book_the_window_skips_rather_than_guessing(
        self, store: Store, clock: FrozenClock
    ) -> None:
        """No book client at all: the window must fire and then skip with NO_QUOTE.
        A runtime that invented a price here would submit against a number nobody
        published — which is the failure the quote gate exists to prevent."""
        run = _runtime(store, clock, book=None)
        _drive_to_intent(run, clock)
        slug = run.rotator.current.slug  # type: ignore[union-attr]
        assert run.rotator.current.window(15).fired_at is not None  # type: ignore[union-attr]
        assert store.intents_for(slug) == ()
        assert store.orders_for(slug) == ()
        assert run._quote(slug, Direction.UP) is None


class TestOneMarketDataPipeline:
    """The runtime owns market data. The adapter is handed it, never fetches it."""

    def test_the_paper_adapter_holds_no_venue_connection(self) -> None:
        """Structural, not behavioural: there is no slot a client could live in.
        A paper adapter that opened its own connection would be a second market
        data pipeline, and V1 would size against a book V2 never saw."""
        assert PaperExecutor.__slots__ == ("_books", "_fills", "_resting", "_sequence")

    def test_the_paper_adapter_reads_nothing_by_itself(
        self, store: Store, clock: FrozenClock
    ) -> None:
        """A bare adapter, never handed a quote, answers with no price at all."""
        assert asyncio.run(PaperExecutor().best_price("any-slug", Direction.UP)) is None

    def test_the_runtime_is_the_only_reader(
        self, store: Store, clock: FrozenClock, book: FakeBook
    ) -> None:
        run = _runtime(store, clock, book=book)
        _drive_to_intent(run, clock)
        assert book.book_reads, "the runtime never read the official book"
        assert set(book.book_reads) == {UP_TOKEN, DOWN_TOKEN}

    def test_the_book_is_read_once_per_refresh_not_once_per_decision(
        self, store: Store, clock: FrozenClock, book: FakeBook
    ) -> None:
        """Twenty passes inside one refresh interval must not be twenty book reads."""
        run = _runtime(store, clock, book=book)
        _pass(run, clock)
        asyncio.run(run._load_tokens(run.rotator.current.slug))  # type: ignore[union-attr]
        clock.advance(_BOOK_REFRESH_SECONDS)
        book.book_reads.clear()
        for _ in range(20):
            asyncio.run(run._refresh_books(clock.now()))
        assert len(book.book_reads) == 2  # one per side, one refresh interval

    def test_the_adapter_is_handed_the_same_price_the_runtime_cached(
        self, store: Store, clock: FrozenClock, book: FakeBook
    ) -> None:
        """`best_price` is what the repricer and the order-book panel read. It has to
        agree with what the strategy sized against, or the two disagree about the
        book within a single pass."""
        run = _runtime(store, clock, book=book)
        _drive_to_intent(run, clock)
        slug = run.rotator.current.slug  # type: ignore[union-attr]
        adapter = asyncio.run(run.executor.best_price(slug, Direction.UP))
        assert adapter == run._quote(slug, Direction.UP) == BEST_BID

    def test_token_ids_load_in_v1_from_the_public_client(
        self, store: Store, clock: FrozenClock, book: FakeBook
    ) -> None:
        """V1 has no secure client. Reading the same book requires the same ids, and
        `get_market` is public data that needs no credential."""
        run = _runtime(store, clock, book=book)
        assert run.venue_client is None
        _pass(run, clock)
        slug = run.rotator.current.slug  # type: ignore[union-attr]
        assert book.market_reads == [slug]
        assert run.tokens(slug, Direction.UP) == UP_TOKEN
        assert run.tokens(slug, Direction.DOWN) == DOWN_TOKEN

    def test_the_same_refresh_path_serves_the_live_adapter(
        self, store: Store, clock: FrozenClock, book: FakeBook
    ) -> None:
        """V2 reads the book through exactly this code. If the refresh were paper-only
        the two modes would differ in more than the adapter."""
        tokens = TokenCache()
        live = LiveExecutor(object(), tokens, lambda _o: None)  # type: ignore[arg-type]
        run = _runtime(store, clock, book=book, executor=live)
        run.tokens = tokens
        # The rotator and the book refresh only. Nothing drives execution here: the
        # live adapter's other calls are authenticated, and this is a claim about
        # the book path, not about V2 order handling.
        run.rotator.advance(clock.now())
        slug = run.rotator.current.slug  # type: ignore[union-attr]
        asyncio.run(run._load_tokens(slug))
        asyncio.run(run._refresh_books(clock.now()))
        assert run._quote(slug, Direction.UP) == BEST_BID


class TestStaleAndMissingQuotes:
    """A price nobody could read is absent. It is never a stale price reused."""

    def _primed(self, store: Store, clock: FrozenClock, book: FakeBook) -> ArcRuntime:
        run = _runtime(store, clock, book=book)
        _pass(run, clock)
        asyncio.run(run._load_tokens(run.rotator.current.slug))  # type: ignore[union-attr]
        asyncio.run(run._refresh_books(clock.now()))
        return run

    def test_a_quote_older_than_the_age_limit_is_reported_absent(
        self, store: Store, clock: FrozenClock, book: FakeBook
    ) -> None:
        run = self._primed(store, clock, book)
        slug = run.rotator.current.slug  # type: ignore[union-attr]
        assert run._quote(slug, Direction.UP) == BEST_BID
        clock.advance(_BOOK_MAX_AGE_SECONDS + 0.01)
        assert run._quote(slug, Direction.UP) is None

    def test_a_quote_inside_the_age_limit_is_still_usable(
        self, store: Store, clock: FrozenClock, book: FakeBook
    ) -> None:
        run = self._primed(store, clock, book)
        slug = run.rotator.current.slug  # type: ignore[union-attr]
        clock.advance(_BOOK_MAX_AGE_SECONDS - 0.01)
        assert run._quote(slug, Direction.UP) == BEST_BID

    def test_one_failed_read_does_not_skip_a_window(
        self, store: Store, clock: FrozenClock, book: FakeBook
    ) -> None:
        """The previous value stays and ages out on its own. Clearing on the first
        error would turn one dropped request into a skipped window."""
        run = self._primed(store, clock, book)
        slug = run.rotator.current.slug  # type: ignore[union-attr]
        book.error = RuntimeError("transport blew up")
        clock.advance(1.0)
        asyncio.run(run._refresh_books(clock.now()))
        assert run._quote(slug, Direction.UP) == BEST_BID

    def test_persistent_failure_does_expire_the_quote(
        self, store: Store, clock: FrozenClock, book: FakeBook
    ) -> None:
        run = self._primed(store, clock, book)
        slug = run.rotator.current.slug  # type: ignore[union-attr]
        book.error = RuntimeError("venue is down")
        for _ in range(10):
            clock.advance(1.0)
            asyncio.run(run._refresh_books(clock.now()))
        assert run._quote(slug, Direction.UP) is None

    def test_an_empty_book_is_not_a_price(
        self, store: Store, clock: FrozenClock
    ) -> None:
        run = _runtime(store, clock, book=FakeBook(bids=()))
        _pass(run, clock)
        asyncio.run(run._load_tokens(run.rotator.current.slug))  # type: ignore[union-attr]
        asyncio.run(run._refresh_books(clock.now()))
        assert run._quote(run.rotator.current.slug, Direction.UP) is None  # type: ignore[union-attr]

    def test_a_failed_read_is_logged_rather_than_swallowed(
        self, store: Store, clock: FrozenClock, book: FakeBook, caplog: pytest.LogCaptureFixture
    ) -> None:
        run = self._primed(store, clock, book)
        book.error = RuntimeError("transport blew up")
        with caplog.at_level(logging.WARNING, logger="arc.test.v1run"):
            clock.advance(1.0)
            asyncio.run(run._refresh_books(clock.now()))
        assert "Book Unavailable" in caplog.text

    def test_the_missing_quote_reaches_the_decision_as_no_quote(
        self, store: Store, clock: FrozenClock
    ) -> None:
        """The skip reason the operator sees. Not a denial: nothing was refused, there
        was simply no book to price against."""
        run = _runtime(store, clock, book=None)
        _drive_to_intent(run, clock)
        market = run.rotator.current
        assert market is not None
        outcome = run._decisions.decide(market, clock.now())
        assert [d.skip for d in outcome.decisions if d.offset_seconds == 15] == [
            SkipReason.NO_QUOTE
        ]


class TestTheBookIsDroppedWithItsMarket:
    def test_an_archived_market_leaves_no_cached_price(
        self, store: Store, clock: FrozenClock, book: FakeBook
    ) -> None:
        """Unbounded growth is the day-two failure. A book kept for every market ever
        traded is a dictionary that grows for as long as the process runs."""
        run = _runtime(store, clock, book=book)
        _pass(run, clock)
        slug = run.rotator.current.slug  # type: ignore[union-attr]
        asyncio.run(run._load_tokens(slug))
        asyncio.run(run._refresh_books(clock.now()))
        assert run._quote(slug, Direction.UP) == BEST_BID

        run._forget_book(slug)
        assert run._quote(slug, Direction.UP) is None
        assert asyncio.run(run.executor.best_price(slug, Direction.UP)) is None

    def test_the_rotation_actually_drops_it(
        self, store: Store, clock: FrozenClock, book: FakeBook
    ) -> None:
        """Two boundaries: market N is archived when N+2 opens."""
        run = _runtime(store, clock, book=book)
        _pass(run, clock)
        first = run.rotator.current.slug  # type: ignore[union-attr]
        asyncio.run(run._load_tokens(first))
        asyncio.run(run._refresh_books(clock.now()))

        for _ in range(2):
            clock.advance(300.0)
            _pass(run, clock)

        assert run.rotator.current is not None
        assert run.rotator.current.slug != first
        assert run._quote(first, Direction.UP) is None


class TestTheWalletFeedsTheBalanceGate:
    """Gate 19's input is refreshed by the loop, exactly as the book's is.

    The decision pass is synchronous, so a gate that awaited a venue call would put
    a round trip inside the freeze. The gate therefore reads an attribute, and this
    is the only thing that writes it.
    """

    def test_v1_publishes_no_balance_and_the_gate_has_no_opinion(
        self, store: Store, clock: FrozenClock, book: FakeBook
    ) -> None:
        """No venue account exists in V1, so `None` is the correct, permanent
        answer. Zero would be a real, denying figure and would stop every paper
        run — which is the evidence the live run depends on."""
        run = _runtime(store, clock, book=book)
        _pass(run, clock)
        assert run.health().available_balance is None
        assert run.health().wallet_connected is True

    def test_a_disconnected_read_closes_the_wallet_gate(
        self, store: Store, clock: FrozenClock, book: FakeBook
    ) -> None:
        run = _runtime(store, clock, book=book)
        _pass(run, clock)
        assert run.health().wallet_connected is True

        run._wallet_status = "DISCONNECTED"
        assert run.health().wallet_connected is False
        assert run.health().wallet_status == "DISCONNECTED"

    def test_the_read_is_on_its_own_clock_not_every_tick(
        self, store: Store, clock: FrozenClock, book: FakeBook
    ) -> None:
        """A venue account call per tick would spend the rate limit submissions
        need. A balance only moves when ARC trades or the operator funds."""
        run = _runtime(store, clock, book=book)
        reads: list[float] = []
        inner = run._wallet

        class Counting:
            async def snapshot(self, now: float, *, run_start: float) -> Any:
                reads.append(now)
                return await inner.snapshot(now, run_start=run_start)

        run._wallet = Counting()  # type: ignore[assignment]
        for _ in range(5):
            clock.advance(0.5)
            asyncio.run(run._refresh_wallet(clock.now()))
        assert len(reads) == 1

        clock.advance(_WALLET_REFRESH_SECONDS)
        asyncio.run(run._refresh_wallet(clock.now()))
        assert len(reads) == 2


class TestNoTradingBehaviourMovedIntoTheAdapter:
    """The refactor added a setter and a dropper. It moved no execution logic."""

    def test_the_adapter_still_fills_only_on_a_real_trade(self) -> None:
        """A paper adapter that filled at submission would report a 100% fill rate
        and hide the single most important execution risk there is."""
        executor = PaperExecutor()
        executor.quote("slug", Direction.UP, Decimal("0.70"))
        assert asyncio.run(executor.fills("slug")) == ()

    def test_setting_a_quote_rests_no_order(self) -> None:
        executor = PaperExecutor()
        executor.quote("slug", Direction.UP, Decimal("0.70"))
        assert asyncio.run(executor.open_orders("slug")) == ()

    def test_forgetting_a_book_cancels_nothing(self, store: Store) -> None:
        """Dropping the price must not touch resting orders — those belong to the
        sweeper, and a cache eviction that cancelled orders would retract a live
        position as a side effect of housekeeping."""
        from execution_fixtures import intent_for, make_market, submitter

        executor = PaperExecutor()
        make_market(store, START_TS)
        slug = f"btc-updown-5m-{START_TS}"
        asyncio.run(
            submitter(store, executor).submit(
                intent_for(window_ts=START_TS),
                count=1,
                phase=MarketPhase.ACTIVE,
                now=float(START_TS + 297),
            )
        )
        executor.quote(slug, Direction.UP, Decimal("0.70"))
        executor.forget(slug)
        assert len(asyncio.run(executor.open_orders(slug))) == 1
