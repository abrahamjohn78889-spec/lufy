"""The observation runtime's PTB acquisition, across consecutive markets.

The property under test is the one the whole PTB design now rests on: a market's
official opening reference is the venue's PUBLISHED `finalPrice` for the market before
it, and that value does not exist when the market opens. Live observation on
2026-08-05 established the timing precisely — the venue writes a market's
`eventMetadata` roughly 25 seconds after it closes, which is roughly 25 seconds into
the NEXT market's life, and 260 seconds before that market's earliest execution window
at close-15s.

That timing is what these tests defend. A runtime that resolved the PTB once, at the
instant the market opened, would find nothing and mark every single market DEAD — and
the failure would read exactly like the venue being down. A runtime that never gave up
would leave a permanently unusable market sitting in a hopeful state forever. The
correct behaviour is bounded retry: try until a PTB could no longer be used, then fail
closed with a recorded reason.

No network here. The discovery client is a scripted fake, so the publication instant
can be placed exactly where the venue puts it and the boundary behaviour is
reproducible rather than observed once.
"""

from __future__ import annotations

import asyncio
import io
import logging
from decimal import Decimal
from pathlib import Path

import pytest
from conftest import VALID_TRADING_VALUES

from arc.clock import FrozenClock
from arc.config import ArcSettings, Settings, TradingConfig, build_trading_config
from arc.domain.enums import MarketPhase
from arc.domain.timing import slug_for, window_ts_for
from arc.errors import FeedError
from arc.market.discovery import MarketMetadata
from arc.market.feed import RtdsFeed
from arc.market.ptb import DEAD_REASON_PTB_UNAVAILABLE, SOURCE_METADATA, SOURCE_PREVIOUS_CLOSE
from arc.runtime.observe import ObservationRun
from arc.runtime.state import RuntimeState
from arc.storage.store import Store

# A grid-aligned start. Three consecutive markets run from here.
START_TS = 1754400000

# Live-measured: the venue publishes a settled market's finalPrice this far after its
# close. Rounded up from the observed ~25s so the tests exercise the retry, not a
# lucky first attempt.
PUBLICATION_DELAY_SECONDS = 25.0


class _Discovery:
    """A scripted metadata source. Publishes finalPrice on the venue's real schedule.

    Holds no network. `fetched` records every slug asked for, in order, which is how
    the tests assert that the runtime looked up the PREVIOUS market rather than
    inventing a value.
    """

    def __init__(
        self,
        *,
        clock: FrozenClock,
        final_prices: dict[int, str],
        ptb_fields: dict[int, str] | None = None,
        publication_delay: float = PUBLICATION_DELAY_SECONDS,
        fail_slugs: set[str] | None = None,
    ) -> None:
        self._clock = clock
        self._final_prices = final_prices
        self._ptb_fields = ptb_fields or {}
        self._publication_delay = publication_delay
        self._fail_slugs = fail_slugs or set()
        self.fetched: list[str] = []

    async def fetch_metadata(self, slug: str) -> MarketMetadata:
        self.fetched.append(slug)
        if slug in self._fail_slugs:
            raise FeedError(f"scripted failure for {slug}")
        window_ts = int(slug.rsplit("-", 1)[1])
        close_ts = window_ts + 300

        # The venue writes eventMetadata when the market settles, not before. Before
        # that instant `finalPrice` is genuinely null.
        published = self._clock.now() >= close_ts + self._publication_delay
        final_price = self._final_prices.get(window_ts) if published else None
        # A scripted PTB field is returned unconditionally. Whether the venue ever
        # populates a live market's own field is exactly what live observation could
        # not establish, so the L1 path is scripted independently rather than tied to
        # the settlement schedule.
        ptb_field = self._ptb_fields.get(window_ts)

        return MarketMetadata(
            slug=slug,
            condition_id=f"0x{window_ts}",
            token_ids=("up", "down"),
            venue_close_ts=close_ts,
            ptb_raw=ptb_field,
            final_price_raw=final_price,
            active=True,
            closed=published,
            raw={},
        )


def _settings() -> Settings:
    trading: TradingConfig = build_trading_config(dict(VALID_TRADING_VALUES))
    return Settings(env=ArcSettings(), trading=trading, seeded_from_env=False)


def _run(store: Store, clock: FrozenClock, discovery: _Discovery) -> ObservationRun:
    settings = _settings()
    runtime = RuntimeState(store, clock)
    runtime.load()
    return ObservationRun(
        settings=settings,
        store=store,
        clock=clock,
        runtime=runtime,
        discovery=discovery,  # type: ignore[arg-type]
        feed=RtdsFeed(clock),
        out=io.StringIO(),
        logger=logging.getLogger("arc.test.observe"),
    )


def _final_prices_for(count: int) -> dict[int, str]:
    """One published close price per market, starting one market before START_TS."""
    return {
        START_TS - 300 + 300 * k: f"{64000 + k}.{k}{k}{k}456789"
        for k in range(count + 1)
    }


def _step(run: ObservationRun, clock: FrozenClock, seconds: float, step: float = 1.0) -> None:
    """Drive the rotation and PTB paths forward through `seconds` of wall clock.

    Deliberately calls advance() and _attempt_ptb() the way the real loop does rather
    than awaiting run.run(): the real loop never terminates, and what is under test is
    the level-triggered behaviour, which is identical at any cadence.
    """
    elapsed = 0.0
    while elapsed < seconds:
        now = clock.now()
        event = run.rotator.advance(now)
        if event.opened:
            run.stats.markets_observed += 1
            run._next_ptb_attempt = 0.0
        asyncio.run(run._attempt_ptb(now))
        clock.advance(step)
        elapsed += step


class TestThePublicationDelay:
    """A market opens BEFORE its opening reference exists. That is normal."""

    def test_a_market_that_just_opened_has_no_ptb_and_is_not_dead(
        self, store: Store
    ) -> None:
        clock = FrozenClock(now=float(START_TS))
        discovery = _Discovery(clock=clock, final_prices=_final_prices_for(2))
        run = _run(store, clock, discovery)

        run.rotator.advance(clock.now())
        asyncio.run(run._attempt_ptb(clock.now()))

        market = run.rotator.current
        assert market is not None
        assert market.ptb is None
        # The critical assertion. Marking it DEAD here would kill every healthy market.
        assert market.phase is not MarketPhase.DEAD
        assert run.stats.ptb_unavailable == 0

    def test_the_ptb_is_frozen_once_the_venue_publishes_it(self, store: Store) -> None:
        clock = FrozenClock(now=float(START_TS))
        discovery = _Discovery(clock=clock, final_prices=_final_prices_for(2))
        run = _run(store, clock, discovery)

        _step(run, clock, 40.0)

        market = run.rotator.current
        assert market is not None
        assert market.ptb == Decimal(_final_prices_for(2)[START_TS - 300])
        assert run.stats.ptb_frozen == 1
        assert run.stats.ptb_unavailable == 0

    def test_the_ptb_arrives_long_before_the_earliest_execution_window(
        self, store: Store
    ) -> None:
        """The margin is what makes the whole approach viable: ~25s to publication
        against 285s until the first window activates at close-15s."""
        clock = FrozenClock(now=float(START_TS))
        discovery = _Discovery(clock=clock, final_prices=_final_prices_for(2))
        run = _run(store, clock, discovery)

        frozen_at: float | None = None
        for _ in range(120):
            now = clock.now()
            run.rotator.advance(now)
            asyncio.run(run._attempt_ptb(now))
            market = run.rotator.current
            if market is not None and market.ptb is not None:
                frozen_at = now
                break
            clock.advance(1.0)

        assert frozen_at is not None
        first_activation = float(START_TS + 300 - 15)
        assert frozen_at < first_activation
        assert first_activation - frozen_at > 200.0

    def test_the_runtime_looks_up_the_previous_market(self, store: Store) -> None:
        """Not the current one, and not a computed value: the settled market before it."""
        clock = FrozenClock(now=float(START_TS))
        discovery = _Discovery(clock=clock, final_prices=_final_prices_for(2))
        run = _run(store, clock, discovery)

        _step(run, clock, 40.0)

        assert slug_for(START_TS - 300) in discovery.fetched


class TestConsecutiveMarkets:
    """The cache handing each market the previous one's published close price."""

    def test_three_consecutive_markets_each_freeze_the_previous_close(
        self, store: Store
    ) -> None:
        clock = FrozenClock(now=float(START_TS))
        prices = _final_prices_for(3)
        discovery = _Discovery(clock=clock, final_prices=prices)
        run = _run(store, clock, discovery)

        frozen: list[tuple[str, Decimal]] = []
        seen: set[str] = set()
        # 3 markets plus enough of the third for its PTB to publish.
        for _ in range(3 * 300 + 60):
            now = clock.now()
            event = run.rotator.advance(now)
            if event.opened:
                run._next_ptb_attempt = 0.0
            asyncio.run(run._attempt_ptb(now))
            market = run.rotator.current
            if market is not None and market.ptb is not None and market.slug not in seen:
                seen.add(market.slug)
                frozen.append((market.slug, market.ptb))
            clock.advance(1.0)

        assert len(frozen) >= 3
        for slug, ptb in frozen[:3]:
            window_ts = int(slug.rsplit("-", 1)[1])
            assert ptb == Decimal(prices[window_ts - 300]), slug

    def test_every_market_gets_a_distinct_ptb(self, store: Store) -> None:
        """A carried-forward value would show up as two markets sharing a PTB, which
        is the failure a stale cache would produce."""
        clock = FrozenClock(now=float(START_TS))
        discovery = _Discovery(clock=clock, final_prices=_final_prices_for(3))
        run = _run(store, clock, discovery)

        values: list[Decimal] = []
        seen: set[str] = set()
        for _ in range(3 * 300 + 60):
            now = clock.now()
            event = run.rotator.advance(now)
            if event.opened:
                run._next_ptb_attempt = 0.0
            asyncio.run(run._attempt_ptb(now))
            market = run.rotator.current
            if market is not None and market.ptb is not None and market.slug not in seen:
                seen.add(market.slug)
                values.append(market.ptb)
            clock.advance(1.0)

        assert len(values) == len(set(values))

    def test_the_frozen_ptb_is_persisted(self, store: Store) -> None:
        """Write-before-act as plain behaviour (A4): a restart must reload the same
        official value rather than re-resolve one."""
        clock = FrozenClock(now=float(START_TS))
        prices = _final_prices_for(2)
        discovery = _Discovery(clock=clock, final_prices=prices)
        run = _run(store, clock, discovery)

        _step(run, clock, 40.0)

        market = run.rotator.current
        assert market is not None
        row = store.load_market_row(market.slug)
        assert row is not None
        assert Decimal(str(row["ptb"])) == Decimal(prices[START_TS - 300])

    def test_the_exact_published_digits_reach_the_market(self, store: Store) -> None:
        clock = FrozenClock(now=float(START_TS))
        exact = "64104.560649297964"
        discovery = _Discovery(clock=clock, final_prices={START_TS - 300: exact})
        run = _run(store, clock, discovery)

        _step(run, clock, 40.0)

        market = run.rotator.current
        assert market is not None
        assert market.ptb == Decimal(exact)
        assert str(market.ptb) == exact


class TestMetadataStillWins:
    """L1 before L2. If the market's own PTB field is populated, it is used."""

    def test_the_markets_own_ptb_field_is_preferred(self, store: Store) -> None:
        clock = FrozenClock(now=float(START_TS))
        discovery = _Discovery(
            clock=clock,
            final_prices={START_TS - 300: "1.00"},
            ptb_fields={START_TS: "99999.25"},
            # Publish immediately so the market's own field is available at once.
            publication_delay=-1.0,
        )
        run = _run(store, clock, discovery)

        _step(run, clock, 5.0)

        market = run.rotator.current
        assert market is not None
        assert market.ptb == Decimal("99999.25")

    def test_the_source_is_recorded_distinctly(self, store: Store) -> None:
        """An operator has to be able to tell which of the two official values was
        used, because they are published at different times by different mechanisms."""
        assert SOURCE_METADATA != SOURCE_PREVIOUS_CLOSE


class TestFailClosed:
    """No official value, no trading. Never an estimate, never a silent default."""

    def test_a_market_whose_reference_never_publishes_goes_dead(self, store: Store) -> None:
        clock = FrozenClock(now=float(START_TS))
        # No final price for the previous market, ever.
        discovery = _Discovery(clock=clock, final_prices={})
        run = _run(store, clock, discovery)

        # Past the first window's activation at close-15s.
        _step(run, clock, 290.0)

        market = run.rotator.current
        assert market is not None
        assert market.phase is MarketPhase.DEAD
        assert market.dead_reason == DEAD_REASON_PTB_UNAVAILABLE
        assert market.ptb is None
        assert run.stats.ptb_unavailable == 1

    def test_it_stays_alive_right_up_to_the_first_activation(self, store: Store) -> None:
        """The deadline is the earliest execution window, not an arbitrary timeout: a
        PTB arriving at close-16s is still usable."""
        clock = FrozenClock(now=float(START_TS))
        discovery = _Discovery(clock=clock, final_prices={})
        run = _run(store, clock, discovery)

        _step(run, clock, 280.0)

        market = run.rotator.current
        assert market is not None
        assert market.phase is not MarketPhase.DEAD

    def test_the_dead_phase_is_persisted_with_its_reason(self, store: Store) -> None:
        clock = FrozenClock(now=float(START_TS))
        discovery = _Discovery(clock=clock, final_prices={})
        run = _run(store, clock, discovery)

        _step(run, clock, 290.0)

        market = run.rotator.current
        assert market is not None
        row = store.load_market_row(market.slug)
        assert row is not None
        assert row["phase"] == MarketPhase.DEAD.value
        assert row["dead_reason"] == DEAD_REASON_PTB_UNAVAILABLE

    def test_a_metadata_request_failure_does_not_end_the_run(self, store: Store) -> None:
        """Operational, not fatal (A8). The process keeps collecting."""
        clock = FrozenClock(now=float(START_TS))
        discovery = _Discovery(
            clock=clock,
            final_prices=_final_prices_for(2),
            fail_slugs={slug_for(START_TS)},
        )
        run = _run(store, clock, discovery)

        _step(run, clock, 40.0)

        market = run.rotator.current
        assert market is not None
        assert market.phase is not MarketPhase.DEAD

    def test_a_market_is_marked_dead_only_once(self, store: Store) -> None:
        clock = FrozenClock(now=float(START_TS))
        discovery = _Discovery(clock=clock, final_prices={})
        run = _run(store, clock, discovery)

        _step(run, clock, 299.0)

        assert run.stats.ptb_unavailable == 1

    def test_the_unavailable_line_is_logged_with_its_reason(
        self, store: Store, caplog: pytest.LogCaptureFixture
    ) -> None:
        clock = FrozenClock(now=float(START_TS))
        discovery = _Discovery(clock=clock, final_prices={})
        run = _run(store, clock, discovery)

        with caplog.at_level(logging.ERROR, logger="arc.test.observe"):
            _step(run, clock, 290.0)

        assert "PTB Unavailable" in caplog.text
        details = [getattr(r, "arc_detail", "") for r in caplog.records]
        assert any("no trading this market" in d for d in details)


class TestRetryBehaviour:
    def test_resolution_is_retried_rather_than_attempted_once(self, store: Store) -> None:
        clock = FrozenClock(now=float(START_TS))
        discovery = _Discovery(clock=clock, final_prices=_final_prices_for(2))
        run = _run(store, clock, discovery)

        _step(run, clock, 40.0)

        # More than one attempt happened, and it stopped once the PTB was in hand.
        assert len(discovery.fetched) > 2
        before = len(discovery.fetched)
        _step(run, clock, 30.0)
        assert len(discovery.fetched) == before

    def test_retries_are_rate_limited(self, store: Store) -> None:
        """Polling every loop pass would issue five requests a second per market."""
        clock = FrozenClock(now=float(START_TS))
        discovery = _Discovery(clock=clock, final_prices={})
        run = _run(store, clock, discovery)

        _step(run, clock, 60.0, step=0.2)

        # 60s at a 5s floor: an order of magnitude below the 300 passes made.
        assert len(discovery.fetched) < 40

    def test_a_new_market_retries_immediately(self, store: Store) -> None:
        """Not after the leftover interval from the market before it."""
        clock = FrozenClock(now=float(START_TS + 299))
        discovery = _Discovery(clock=clock, final_prices=_final_prices_for(2))
        run = _run(store, clock, discovery)

        run.rotator.advance(clock.now())
        asyncio.run(run._attempt_ptb(clock.now()))
        before = len(discovery.fetched)

        clock.advance(1.0)
        event = run.rotator.advance(clock.now())
        assert event.opened
        run._next_ptb_attempt = 0.0
        asyncio.run(run._attempt_ptb(clock.now()))

        assert len(discovery.fetched) > before

    def test_a_frozen_market_is_never_re_resolved(self, store: Store) -> None:
        """freeze_ptb raises on a second call (A11/A12), so a re-resolution attempt
        would crash the loop rather than silently refresh."""
        clock = FrozenClock(now=float(START_TS))
        discovery = _Discovery(clock=clock, final_prices=_final_prices_for(2))
        run = _run(store, clock, discovery)

        _step(run, clock, 200.0)

        market = run.rotator.current
        assert market is not None
        assert market.ptb is not None
        assert run.stats.ptb_frozen == 1


class TestNoBoundaryPathRemains:
    """Structural: the observation-based PTB source must be gone, not just unused.

    Live measurement showed ARC's boundary observation differs from the venue's
    published close price by 6E-12. Leaving the path in place, even unreachable, is
    the kind of thing a later change re-enables by accident — and an estimated PTB
    produces confident wrong trades with nothing in the record showing a number had
    been invented.
    """

    def test_the_runtime_holds_no_boundary_reference(self, source_root: Path) -> None:
        text = (source_root / "arc" / "runtime" / "observe.py").read_text(encoding="utf-8")
        assert "BoundaryReference" not in text
        assert "_record_boundary" not in text

    def test_the_ptb_module_names_no_boundary_source(self, source_root: Path) -> None:
        text = (source_root / "arc" / "market" / "ptb.py").read_text(encoding="utf-8")
        assert "BoundaryReference" not in text

    def test_the_feed_tracks_no_boundary_continuity(self, source_root: Path) -> None:
        text = (source_root / "arc" / "market" / "feed.py").read_text(encoding="utf-8")
        assert "BoundaryTracker" not in text
        assert "observe_boundary" not in text

    def test_the_grid_arithmetic_is_unchanged(self) -> None:
        """The contiguity the cache relies on is the same A5 grid, not a new rule."""
        assert window_ts_for(float(START_TS + 1)) == START_TS
        assert slug_for(START_TS) == f"btc-updown-5m-{START_TS}"
