"""Continuous runtime: 500 consecutive markets through the real engine.

Not a smoke test. The whole point of a 24/7 bot is the five-hundredth market, not
the first: leaks, drift, duplicate accounting and unbounded growth are invisible in
a three-market run and fatal in a two-day one.

Every component is real — the Store on a real SQLite file, the Submitter, the
FillEngine, the Sweeper, the Reconciler, the order FSM and the V1 paper adapter,
which is production code. Nothing is stubbed and no clock is read; time is supplied
by the loop, so the run is reproducible and a second identical run must produce a
byte-identical digest.
"""

from __future__ import annotations

import asyncio
import hashlib
import tracemalloc
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

import pytest
from execution_fixtures import (
    LIMIT_PRICE,
    OFFSETS,
    WINDOW_TS,
    fill_engine,
    intent_for,
    make_market,
    reconciler,
    store_at,
    submitter,
    sweeper,
)

from arc.domain.enums import Direction, MarketPhase, OrderState, Outcome
from arc.domain.models import Settlement
from arc.domain.timing import slug_for
from arc.execution.v1_paper import PaperExecutor
from arc.runtime.recovery import RecoveryRunner
from arc.storage.store import Store

MARKETS = 500
SIZE = Decimal("36")
RESTART_AT = 250


@dataclass
class Tally:
    """Counters the run is judged on. Every one of them is a duplicate detector."""

    markets: list[str] = field(default_factory=list)
    submitted: list[str] = field(default_factory=list)
    fill_ids: list[str] = field(default_factory=list)
    settled: list[str] = field(default_factory=list)
    duplicate_settlements: int = 0
    filled_total: Decimal = Decimal("0")

    def digest(self) -> str:
        payload = "|".join(
            (
                ",".join(self.markets),
                ",".join(self.submitted),
                ",".join(self.fill_ids),
                ",".join(self.settled),
                str(self.filled_total),
            )
        )
        return hashlib.sha256(payload.encode()).hexdigest()


def _plan(index: int) -> tuple[int, int, Direction, Decimal]:
    """Deterministic per-market variation. No randomness: a run that cannot be
    replayed cannot be used as evidence."""
    offset = OFFSETS[index % len(OFFSETS)]
    count = (index % 3) + 1
    direction = Direction.UP if index % 2 == 0 else Direction.DOWN
    # 0 -> no fill at all (the BUFFER_NOT_SATISFIED path), 1 -> partial, 2 -> full.
    traded = (Decimal("0"), SIZE / 3, SIZE)[index % 3]
    return offset, count, direction, traded


def _one_market(store: Store, executor: PaperExecutor, index: int, tally: Tally) -> None:
    """One complete market: open -> submit -> fill -> sweep -> settle -> drop."""
    window_ts = WINDOW_TS + index * 300
    slug = slug_for(window_ts)
    offset, count, direction, traded = _plan(index)
    now = float(window_ts + 300 - offset)

    market = make_market(store, window_ts)
    store.save_phase(slug, MarketPhase.ACTIVE, now)
    tally.markets.append(slug)

    intent = intent_for(
        window_ts=window_ts,
        offset_seconds=offset,
        direction=direction,
        size=SIZE,
    )
    orders = asyncio.run(
        submitter(store, executor).submit(
            intent, count=count, phase=MarketPhase.ACTIVE, now=now
        )
    )
    tally.submitted.extend(o.order_id for o in orders)

    if traded > Decimal("0"):
        executor.trade(slug, LIMIT_PRICE, traded)
    engine = fill_engine(store, executor)
    report = asyncio.run(engine.poll(slug, now + 1))
    tally.fill_ids.extend(f.fill_id for f in report.new_fills)
    tally.filled_total += engine.filled_for_window(slug, offset)

    # The sweep is driven by phase, never by a clock (A10/D1).
    store.save_phase(slug, MarketPhase.CANCELLING, float(window_ts + 299))
    sweep = asyncio.run(sweeper(store, executor).sweep(slug, float(window_ts + 299)))
    assert sweep.clean, (slug, sweep.unknown)

    store.save_phase(slug, MarketPhase.SETTLING, float(window_ts + 300))
    settlement = Settlement(
        market_slug=slug,
        outcome=Outcome.UP if direction is Direction.UP else Outcome.DOWN,
        settlement_twap=Decimal("64010.00"),
        ptb=Decimal("64000.00"),
        settled_at=float(window_ts + 301),
    )
    if store.save_settlement(settlement):
        tally.settled.append(slug)
    if store.save_settlement(settlement):
        # A second acceptance of the same resolution would double realised P/L.
        tally.duplicate_settlements += 1
    store.save_phase(slug, MarketPhase.SETTLED, float(window_ts + 302))
    store.archive_market(slug, float(window_ts + 302))
    del market  # A11: the instance is dropped at close, never reset and reused.


def _run(store: Store, executor: PaperExecutor, count: int, tally: Tally) -> None:
    for index in range(count):
        _one_market(store, executor, index, tally)


# ── the run itself, executed once and shared by every assertion ──────────────


@pytest.fixture(scope="module")
def marathon(tmp_path_factory: pytest.TempPathFactory):  # type: ignore[no-untyped-def]
    """500 markets, with a mid-run restart. Module-scoped: run once, assert many."""
    tmp_path = tmp_path_factory.mktemp("marathon")
    executor = PaperExecutor()
    tally = Tally()

    tracemalloc.start()
    store = store_at(tmp_path)
    for index in range(RESTART_AT):
        _one_market(store, executor, index, tally)
    after_first_half, _ = tracemalloc.get_traced_memory()

    # PM2 restart / VPS reboot: same database file, same venue, new process state.
    store.close()
    store = store_at(tmp_path)
    report = asyncio.run(
        RecoveryRunner(
            store, reconciler(store, executor), fill_engine(store, executor)
        ).run(float(WINDOW_TS + RESTART_AT * 300))
    )

    for index in range(RESTART_AT, MARKETS):
        _one_market(store, executor, index, tally)
    after_second_half, _ = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    yield store, executor, tally, report, after_first_half, after_second_half
    store.close()


class TestNoMarketIsSkipped:
    def test_every_market_on_the_grid_ran(self, marathon) -> None:  # type: ignore[no-untyped-def]
        store, _executor, tally, _report, _a, _b = marathon
        assert len(tally.markets) == MARKETS
        assert store.market_count() == MARKETS

    def test_the_slugs_are_contiguous_on_the_five_minute_grid(self, marathon) -> None:  # type: ignore[no-untyped-def]
        _store, _executor, tally, _report, _a, _b = marathon
        expected = [slug_for(WINDOW_TS + i * 300) for i in range(MARKETS)]
        assert tally.markets == expected

    def test_every_market_reached_a_terminal_phase(self, marathon) -> None:  # type: ignore[no-untyped-def]
        store, _executor, _tally, _report, _a, _b = marathon
        assert store.unsettled_markets() == ()


class TestNoDuplicates:
    def test_no_order_id_was_submitted_twice(self, marathon) -> None:  # type: ignore[no-untyped-def]
        _store, _executor, tally, _report, _a, _b = marathon
        assert len(set(tally.submitted)) == len(tally.submitted)

    def test_no_fill_id_was_recorded_twice(self, marathon) -> None:  # type: ignore[no-untyped-def]
        _store, _executor, tally, _report, _a, _b = marathon
        assert len(set(tally.fill_ids)) == len(tally.fill_ids)

    def test_no_market_settled_twice(self, marathon) -> None:  # type: ignore[no-untyped-def]
        _store, _executor, tally, _report, _a, _b = marathon
        assert tally.duplicate_settlements == 0
        assert len(set(tally.settled)) == MARKETS

    def test_persisted_fills_match_what_the_run_saw(self, marathon) -> None:  # type: ignore[no-untyped-def]
        store, _executor, tally, _report, _a, _b = marathon
        stored = sum(
            len(store.fills_for(slug)) for slug in tally.markets
        )
        assert stored == len(tally.fill_ids)


class TestNothingIsLeaked:
    def test_no_order_is_still_live_anywhere(self, marathon) -> None:  # type: ignore[no-untyped-def]
        store, _executor, _tally, _report, _a, _b = marathon
        assert store.live_orders() == ()

    def test_the_venue_holds_nothing(self, marathon) -> None:  # type: ignore[no-untyped-def]
        """500 markets of sweeps: the simulated book must be empty at the end."""
        _store, executor, tally, _report, _a, _b = marathon
        resting = [
            o for slug in tally.markets for o in asyncio.run(executor.open_orders(slug))
        ]
        assert resting == []

    def test_every_order_reached_a_terminal_state(self, marathon) -> None:  # type: ignore[no-untyped-def]
        store, _executor, tally, _report, _a, _b = marathon
        live = {OrderState.PENDING, OrderState.SUBMITTED, OrderState.PARTIAL}
        offenders = [
            o.order_id
            for slug in tally.markets
            for o in store.orders_for(slug)
            if o.state in live or o.state is OrderState.INDETERMINATE
        ]
        assert offenders == []

    def test_no_position_is_orphaned(self, marathon) -> None:  # type: ignore[no-untyped-def]
        """Every fill belongs to an order row and to a settled market."""
        store, _executor, tally, _report, _a, _b = marathon
        for slug in tally.markets:
            known = {o.order_id for o in store.orders_for(slug)}
            fills = store.fills_for(slug)
            assert all(f.order_id in known for f in fills), slug
            if fills:
                assert store.settlement_for(slug) is not None, slug


class TestAccounting:
    def test_filled_quantity_never_exceeds_the_approved_exposure(self, marathon) -> None:  # type: ignore[no-untyped-def]
        store, _executor, tally, _report, _a, _b = marathon
        for index, slug in enumerate(tally.markets):
            offset, _count, _direction, _traded = _plan(index)
            assert store.filled_size_for_window(slug, offset) <= SIZE, slug

    def test_the_expected_quantity_actually_filled(self, marathon) -> None:  # type: ignore[no-untyped-def]
        """Guards the run itself: a marathon where nothing fills proves nothing."""
        _store, _executor, tally, _report, _a, _b = marathon
        expected = sum(
            (_plan(i)[3] for i in range(MARKETS)), Decimal("0")
        )
        assert tally.filled_total == expected
        assert tally.filled_total > Decimal("0")

    def test_a_third_of_the_markets_never_filled(self, marathon) -> None:  # type: ignore[no-untyped-def]
        """The BUFFER_NOT_SATISFIED path is exercised, not merely available."""
        store, _executor, tally, _report, _a, _b = marathon
        unfilled = [
            slug
            for index, slug in enumerate(tally.markets)
            if store.filled_size_for_window(slug, _plan(index)[0]) == Decimal("0")
        ]
        assert len(unfilled) == len(range(0, MARKETS, 3))


class TestRestartAndStability:
    def test_the_mid_run_restart_recovered_cleanly(self, marathon) -> None:  # type: ignore[no-untyped-def]
        _store, _executor, _tally, report, _a, _b = marathon
        assert report.safe_to_trade
        assert report.orphans == ()

    def test_the_markets_after_the_restart_behaved_identically(self, marathon) -> None:  # type: ignore[no-untyped-def]
        """The restart must not be visible in the output."""
        store, _executor, tally, _report, _a, _b = marathon
        for index in (RESTART_AT - 3, RESTART_AT, RESTART_AT + 3):
            offset, count, _direction, traded = _plan(index)
            slug = tally.markets[index]
            assert len(store.orders_for(slug)) == count
            assert store.filled_size_for_window(slug, offset) == traded

    def test_memory_does_not_grow_with_the_number_of_markets(self, marathon) -> None:  # type: ignore[no-untyped-def]
        """Per-market state lives on the instance and is dropped at close (A11).

        A leak here is the failure that only shows up on day two of a VPS run, by
        which point it has taken the process down with it.
        """
        _store, _executor, _tally, _report, first, second = marathon
        assert second - first < 2_000_000, (first, second)


class TestDeterminism:
    def test_a_second_identical_run_produces_the_same_digest(
        self, tmp_path: Path, marathon
    ) -> None:  # type: ignore[no-untyped-def]
        """Same inputs, same order ids, same fill ids, same totals.

        Shortened to a tenth of the marathon: determinism is a property of the
        per-market path, and running it fifty times exercises every branch of
        `_plan` ten times over.
        """
        _store, _executor, _tally, _report, _a, _b = marathon
        digests = []
        for name in ("run-a.db", "run-b.db"):
            store = store_at(tmp_path, name)
            tally = Tally()
            _run(store, PaperExecutor(), 50, tally)
            digests.append(tally.digest())
            store.close()
        assert digests[0] == digests[1]
