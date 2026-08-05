"""submission_count divides the approved exposure. It never multiplies it.

The frozen contract: N submissions sum to exactly the single approved
ExecutionIntent exposure, each submission is managed independently, fills from all
of them accumulate toward the same window position, rounding is distributed
deterministically, and N is reduced rather than emitting an order below the venue
minimum.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal
from pathlib import Path

import pytest
from conftest import VALID_TRADING_VALUES
from execution_fixtures import (
    LIMIT_PRICE,
    MINIMUM,
    WINDOW_TS,
    fill_engine,
    intent_for,
    make_market,
    store_at,
    submitter,
)

from arc.config import build_trading_config
from arc.domain.enums import MarketPhase, OrderState
from arc.errors import ConfigInvariantError
from arc.execution.orders import order_id_for
from arc.execution.submit import SplitError, split_size
from arc.execution.v1_paper import PaperExecutor

NOW = float(WINDOW_TS + 297)
STEP = Decimal("1")


def _split(total: str, count: int, minimum: str = "5", step: str = "1"):  # type: ignore[no-untyped-def]
    return split_size(
        Decimal(total), count, minimum=Decimal(minimum), size_step=Decimal(step)
    )


class TestTheSplitSumsToTheApprovedSize:
    @pytest.mark.parametrize("count", [1, 2, 3, 4, 5, 6, 7])
    def test_every_count_sums_to_exactly_the_approved_total(self, count: int) -> None:
        """The whole point of the contract. More orders must not mean more exposure."""
        sizes = _split("35", count)
        assert sum(sizes) == Decimal("35")

    @pytest.mark.parametrize(
        ("total", "count", "expected"),
        [
            ("35", 1, (Decimal("35"),)),
            ("35", 2, (Decimal("18"), Decimal("17"))),
            ("35", 3, (Decimal("12"), Decimal("12"), Decimal("11"))),
            ("30", 3, (Decimal("10"), Decimal("10"), Decimal("10"))),
            ("31", 3, (Decimal("11"), Decimal("10"), Decimal("10"))),
        ],
    )
    def test_the_remainder_lands_on_the_earliest_splits(
        self, total: str, count: int, expected: tuple[Decimal, ...]
    ) -> None:
        """Any fixed rule would do; it must be THE SAME rule on every replay.

        A replay that produced different sizes would produce different order ids,
        and the venue would receive a second, distinct order.
        """
        assert _split(total, count) == expected

    def test_the_same_input_produces_the_same_ladder_every_time(self) -> None:
        assert len({_split("37", 4) for _ in range(50)}) == 1


class TestTheVenueMinimumReducesN:
    def test_a_count_that_would_go_under_the_minimum_is_reduced(self) -> None:
        """12 shares cannot be five valid orders of 5; it can be two of 6."""
        assert _split("12", 5) == (Decimal("6"), Decimal("6"))

    def test_a_total_below_the_minimum_produces_no_orders_at_all(self) -> None:
        assert _split("4", 1) == ()

    def test_every_split_clears_the_venue_minimum(self) -> None:
        for total in range(5, 60):
            for count in range(1, 8):
                sizes = _split(str(total), count)
                assert all(s >= MINIMUM for s in sizes), (total, count, sizes)
                assert sum(sizes) == Decimal(total)

    def test_a_non_positive_count_is_refused(self) -> None:
        with pytest.raises(SplitError):
            _split("35", 0)

    def test_a_non_positive_size_is_refused(self) -> None:
        with pytest.raises(SplitError):
            _split("0", 1)


class TestQuantization:
    def test_a_coarse_size_step_never_produces_an_unquantized_order(self) -> None:
        sizes = _split("37", 3, minimum="5", step="5")
        assert all(s % Decimal("5") == Decimal("0") for s in sizes)
        # Sub-step dust is dropped rather than added to a split, which would make
        # that split unquantized and the venue would reject it.
        assert sum(sizes) <= Decimal("37")

    def test_the_total_is_never_exceeded(self) -> None:
        for total in range(5, 80):
            for count in range(1, 6):
                assert sum(_split(str(total), count, step="5")) <= Decimal(total)


@pytest.fixture
def wired(tmp_path: Path):  # type: ignore[no-untyped-def]
    store = store_at(tmp_path)
    market = make_market(store)
    executor = PaperExecutor()
    yield store, executor, market
    store.close()


class TestSubmissionThroughTheEngine:
    def test_default_of_one_places_a_single_order_for_the_whole_size(self, wired) -> None:  # type: ignore[no-untyped-def]
        store, executor, _market = wired
        orders = asyncio.run(
            submitter(store, executor).submit(
                intent_for(), count=1, phase=MarketPhase.ACTIVE, now=NOW
            )
        )
        assert len(orders) == 1
        assert orders[0].size == Decimal("35")

    def test_three_submissions_place_three_orders_summing_to_the_approved_size(
        self, wired
    ) -> None:  # type: ignore[no-untyped-def]
        store, executor, _market = wired
        orders = asyncio.run(
            submitter(store, executor).submit(
                intent_for(), count=3, phase=MarketPhase.ACTIVE, now=NOW
            )
        )
        assert len(orders) == 3
        assert sum(o.size for o in orders) == Decimal("35")
        assert all(o.state is OrderState.SUBMITTED for o in orders)

    def test_every_submission_carries_the_intents_price_and_direction_unchanged(
        self, wired
    ) -> None:  # type: ignore[no-untyped-def]
        store, executor, _market = wired
        intent = intent_for()
        orders = asyncio.run(
            submitter(store, executor).submit(
                intent, count=3, phase=MarketPhase.ACTIVE, now=NOW
            )
        )
        assert {o.price for o in orders} == {intent.limit_price}
        assert {o.direction for o in orders} == {intent.direction}
        assert {o.offset_seconds for o in orders} == {intent.offset_seconds}

    def test_the_submissions_belong_to_one_window_by_derived_id(self, wired) -> None:  # type: ignore[no-untyped-def]
        store, executor, market = wired
        asyncio.run(
            submitter(store, executor).submit(
                intent_for(), count=3, phase=MarketPhase.ACTIVE, now=NOW
            )
        )
        expected = {order_id_for(market.slug, 3, i, 0) for i in range(3)}
        assert {o.order_id for o in store.orders_for(market.slug)} == expected

    def test_each_submission_is_managed_independently(self, wired) -> None:  # type: ignore[no-untyped-def]
        """One filling does not touch the others."""
        store, executor, market = wired
        asyncio.run(
            submitter(store, executor).submit(
                intent_for(), count=3, phase=MarketPhase.ACTIVE, now=NOW
            )
        )
        executor.trade(market.slug, LIMIT_PRICE, Decimal("12"))
        asyncio.run(fill_engine(store, executor).poll(market.slug, NOW))

        states = {o.order_id: o.state for o in store.orders_for(market.slug)}
        assert states[order_id_for(market.slug, 3, 0, 0)] is OrderState.FILLED
        assert states[order_id_for(market.slug, 3, 1, 0)] is OrderState.SUBMITTED
        assert states[order_id_for(market.slug, 3, 2, 0)] is OrderState.SUBMITTED

    def test_fills_from_every_submission_accumulate_to_one_window_position(
        self, wired
    ) -> None:  # type: ignore[no-untyped-def]
        store, executor, market = wired
        asyncio.run(
            submitter(store, executor).submit(
                intent_for(), count=3, phase=MarketPhase.ACTIVE, now=NOW
            )
        )
        executor.trade(market.slug, LIMIT_PRICE, Decimal("35"))
        engine = fill_engine(store, executor)
        asyncio.run(engine.poll(market.slug, NOW))

        assert engine.filled_for_window(market.slug, 3) == Decimal("35")

    def test_a_fill_never_creates_exposure_beyond_the_approved_total(self, wired) -> None:  # type: ignore[no-untyped-def]
        """A counterparty willing to trade far more than the approved size."""
        store, executor, market = wired
        asyncio.run(
            submitter(store, executor).submit(
                intent_for(), count=5, phase=MarketPhase.ACTIVE, now=NOW
            )
        )
        executor.trade(market.slug, LIMIT_PRICE, Decimal("500"))
        engine = fill_engine(store, executor)
        asyncio.run(engine.poll(market.slug, NOW))

        assert engine.filled_for_window(market.slug, 3) == Decimal("35")

    def test_a_count_beyond_the_minimum_is_reduced_at_submission_too(self, wired) -> None:  # type: ignore[no-untyped-def]
        store, executor, _market = wired
        orders = asyncio.run(
            submitter(store, executor).submit(
                intent_for(size=Decimal("12")),
                count=5,
                phase=MarketPhase.ACTIVE,
                now=NOW,
            )
        )
        assert [o.size for o in orders] == [Decimal("6"), Decimal("6")]

    def test_a_size_below_the_minimum_submits_nothing(self, wired) -> None:  # type: ignore[no-untyped-def]
        store, executor, market = wired
        orders = asyncio.run(
            submitter(store, executor).submit(
                intent_for(size=Decimal("3")),
                count=1,
                phase=MarketPhase.ACTIVE,
                now=NOW,
            )
        )
        assert orders == ()
        assert store.orders_for(market.slug) == ()


class TestSubmissionCountConfiguration:
    def test_the_default_is_one(self) -> None:
        assert build_trading_config(dict(VALID_TRADING_VALUES)).submission_count == 1

    def test_zero_is_refused(self) -> None:
        values = dict(VALID_TRADING_VALUES, submission_count="0")
        with pytest.raises(ConfigInvariantError, match="at least 1"):
            build_trading_config(values)

    def test_a_count_beyond_the_affordable_minimums_is_refused_at_boot(self) -> None:
        """POSITION_NOTIONAL_USD 25 at ENTRY_PRICE_MAX 0.85 buys 29 shares: 5 splits."""
        values = dict(VALID_TRADING_VALUES, submission_count="99")
        with pytest.raises(ConfigInvariantError, match="exceeds"):
            build_trading_config(values)

    def test_a_valid_higher_count_is_accepted(self) -> None:
        values = dict(VALID_TRADING_VALUES, submission_count="3")
        assert build_trading_config(values).submission_count == 3
