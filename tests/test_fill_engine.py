"""Fill monitoring: submission is not a position.

The property under test throughout is that filled quantity comes from the venue's
own fill records, is idempotent on the venue's fill id, and never advances merely
because an order was accepted.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal
from pathlib import Path

import pytest
from execution_fixtures import (
    LIMIT_PRICE,
    WINDOW_TS,
    fill_engine,
    intent_for,
    make_market,
    store_at,
    submitter,
)

from arc.domain.enums import Direction, MarketPhase, OrderState
from arc.domain.models import Fill
from arc.execution.v1_paper import PaperExecutor
from arc.storage.store import Store

NOW = float(WINDOW_TS + 297)


@pytest.fixture
def wired(tmp_path: Path):  # type: ignore[no-untyped-def]
    store = store_at(tmp_path)
    market = make_market(store)
    executor = PaperExecutor()
    yield store, executor, market
    store.close()


def _submit(store: Store, executor: PaperExecutor, **kwargs: object):  # type: ignore[no-untyped-def]
    intent = intent_for(**kwargs)  # type: ignore[arg-type]
    return asyncio.run(
        submitter(store, executor).submit(
            intent, count=1, phase=MarketPhase.ACTIVE, now=NOW
        )
    )


class TestSubmissionIsNotAFill:
    def test_a_submitted_order_has_no_filled_quantity(self, wired) -> None:  # type: ignore[no-untyped-def]
        store, executor, market = wired
        orders = _submit(store, executor)

        assert orders[0].state is OrderState.SUBMITTED
        assert orders[0].filled_size == Decimal("0")
        assert store.filled_size_for_window(market.slug, 3) == Decimal("0")

    def test_the_paper_venue_never_invents_a_fill(self, wired) -> None:  # type: ignore[no-untyped-def]
        """An adapter that filled on submission would report a 100% fill rate."""
        store, executor, market = wired
        _submit(store, executor)

        report = asyncio.run(fill_engine(store, executor).poll(market.slug, NOW))
        assert report.new_fills == ()


class TestFillsAdvanceTheOrder:
    def test_a_partial_fill_moves_the_order_to_partial(self, wired) -> None:  # type: ignore[no-untyped-def]
        store, executor, market = wired
        _submit(store, executor)
        executor.trade(market.slug, LIMIT_PRICE, Decimal("10"))

        report = asyncio.run(fill_engine(store, executor).poll(market.slug, NOW))

        assert report.filled_size == Decimal("10")
        stored = store.orders_for(market.slug)[0]
        assert stored.state is OrderState.PARTIAL
        assert stored.filled_size == Decimal("10")
        assert stored.remaining_size == Decimal("25")

    def test_a_complete_fill_moves_the_order_to_filled(self, wired) -> None:  # type: ignore[no-untyped-def]
        store, executor, market = wired
        _submit(store, executor)
        executor.trade(market.slug, LIMIT_PRICE, Decimal("35"))

        asyncio.run(fill_engine(store, executor).poll(market.slug, NOW))

        stored = store.orders_for(market.slug)[0]
        assert stored.state is OrderState.FILLED
        assert stored.remaining_size == Decimal("0")
        assert not stored.is_live

    def test_a_passive_order_above_the_trade_price_does_not_fill(self, wired) -> None:  # type: ignore[no-untyped-def]
        """The unfilled outcome the engine has to handle correctly."""
        store, executor, market = wired
        _submit(store, executor, limit_price=Decimal("0.60"))
        executor.trade(market.slug, Decimal("0.75"), Decimal("35"))

        report = asyncio.run(fill_engine(store, executor).poll(market.slug, NOW))

        assert report.new_fills == ()
        assert store.orders_for(market.slug)[0].state is OrderState.SUBMITTED


class TestFillsAreIdempotent:
    def test_repolling_the_same_fill_does_not_double_the_position(self, wired) -> None:  # type: ignore[no-untyped-def]
        """The executor returns the WHOLE history on every poll, by design."""
        store, executor, market = wired
        _submit(store, executor)
        executor.trade(market.slug, LIMIT_PRICE, Decimal("10"))
        engine = fill_engine(store, executor)

        first = asyncio.run(engine.poll(market.slug, NOW))
        second = asyncio.run(engine.poll(market.slug, NOW + 1))
        third = asyncio.run(engine.poll(market.slug, NOW + 2))

        assert first.filled_size == Decimal("10")
        assert second.new_fills == ()
        assert third.new_fills == ()
        assert store.orders_for(market.slug)[0].filled_size == Decimal("10")

    def test_a_websocket_redelivery_is_rejected_by_the_database(self, wired) -> None:  # type: ignore[no-untyped-def]
        """Novelty is arbitrated by SQLite, not by an in-memory set.

        An in-memory set is empty again after a restart, at which point every
        historical fill would re-apply and double the recorded position.
        """
        store, executor, market = wired
        orders = _submit(store, executor)
        engine = fill_engine(store, executor)
        pushed = (
            Fill(
                fill_id="venue-1",
                order_id=orders[0].order_id,
                market_slug=market.slug,
                size=Decimal("12"),
                price=LIMIT_PRICE,
                ts=NOW,
            ),
        )

        assert engine.ingest(market.slug, pushed, NOW).filled_size == Decimal("12")
        assert engine.ingest(market.slug, pushed, NOW).new_fills == ()
        assert store.orders_for(market.slug)[0].filled_size == Decimal("12")

    def test_a_zero_size_fill_is_ignored(self, wired) -> None:  # type: ignore[no-untyped-def]
        store, executor, market = wired
        orders = _submit(store, executor)
        pushed = (
            Fill(
                fill_id="venue-0",
                order_id=orders[0].order_id,
                market_slug=market.slug,
                size=Decimal("0"),
                price=LIMIT_PRICE,
                ts=NOW,
            ),
        )
        assert fill_engine(store, executor).ingest(market.slug, pushed, NOW).new_fills == ()


class TestFillAccounting:
    def test_quantity_is_summed_across_the_whole_reprice_chain(self, wired) -> None:  # type: ignore[no-untyped-def]
        """Five sub-minimum fills are one position, not five (hazard H4)."""
        store, executor, market = wired
        _submit(store, executor)
        for _ in range(5):
            executor.trade(market.slug, LIMIT_PRICE, Decimal("7"))
        engine = fill_engine(store, executor)
        asyncio.run(engine.poll(market.slug, NOW))

        assert engine.filled_for_window(market.slug, 3) == Decimal("35")

    def test_unfilled_lists_only_live_orders_with_quantity_outstanding(
        self, wired
    ) -> None:  # type: ignore[no-untyped-def]
        store, executor, market = wired
        _submit(store, executor)
        engine = fill_engine(store, executor)

        assert len(engine.unfilled(market.slug)) == 1

        executor.trade(market.slug, LIMIT_PRICE, Decimal("35"))
        asyncio.run(engine.poll(market.slug, NOW))

        assert engine.unfilled(market.slug) == ()

    def test_a_fill_for_an_unknown_order_is_still_recorded(self, wired) -> None:  # type: ignore[no-untyped-def]
        """Real money. Dropping it would understate the position permanently."""
        store, executor, market = wired
        orders = _submit(store, executor)
        # Foreign key: the fill must point at an order row that exists, so the
        # unlinked case is modelled by a fill this process's ORDER MAP has not seen
        # — a fill arriving for an order written by an earlier run.
        pushed = (
            Fill(
                fill_id="venue-9",
                order_id=orders[0].order_id,
                market_slug=market.slug,
                size=Decimal("3"),
                price=LIMIT_PRICE,
                ts=NOW,
            ),
        )
        engine = fill_engine(store, executor)
        assert engine.ingest(market.slug, pushed, NOW).filled_size == Decimal("3")
        assert len(store.fills_for(market.slug)) == 1


class TestDirectionSafety:
    """UP/DOWN tokens trade on separate books; cross-side activity must not fill."""

    def test_up_resting_order_does_not_fill_on_down_activity(self, wired) -> None:  # type: ignore[no-untyped-def]
        store, executor, market = wired
        _submit(store, executor, direction=Direction.UP)
        fills = executor.trade(market.slug, LIMIT_PRICE, Decimal("35"), direction=Direction.DOWN)
        assert fills == ()
        assert store.orders_for(market.slug)[0].state is OrderState.SUBMITTED

    def test_down_resting_order_does_not_fill_on_up_activity(self, wired) -> None:  # type: ignore[no-untyped-def]
        store, executor, market = wired
        _submit(store, executor, direction=Direction.DOWN)
        fills = executor.trade(market.slug, LIMIT_PRICE, Decimal("35"), direction=Direction.UP)
        assert fills == ()
        assert store.orders_for(market.slug)[0].state is OrderState.SUBMITTED

    def test_correct_side_activity_fills_an_up_order(self, wired) -> None:  # type: ignore[no-untyped-def]
        store, executor, market = wired
        _submit(store, executor, direction=Direction.UP)
        fills = executor.trade(market.slug, LIMIT_PRICE, Decimal("35"), direction=Direction.UP)
        assert len(fills) == 1
        assert fills[0].size == Decimal("35")
        asyncio.run(fill_engine(store, executor).poll(market.slug, NOW))
        assert store.orders_for(market.slug)[0].state is OrderState.FILLED

    def test_correct_side_activity_fills_a_down_order(self, wired) -> None:  # type: ignore[no-untyped-def]
        store, executor, market = wired
        _submit(store, executor, direction=Direction.DOWN)
        fills = executor.trade(market.slug, LIMIT_PRICE, Decimal("35"), direction=Direction.DOWN)
        assert len(fills) == 1
        assert fills[0].size == Decimal("35")
        asyncio.run(fill_engine(store, executor).poll(market.slug, NOW))
        assert store.orders_for(market.slug)[0].state is OrderState.FILLED

    def test_undirected_trade_still_fills_for_backward_compatibility(self, wired) -> None:  # type: ignore[no-untyped-def]
        """Existing tests omit direction; the bridge always passes it, but old callers don't."""
        store, executor, market = wired
        _submit(store, executor, direction=Direction.UP)
        fills = executor.trade(market.slug, LIMIT_PRICE, Decimal("35"))
        assert len(fills) == 1
