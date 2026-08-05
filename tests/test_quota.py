"""The trade quota: hazard H2 (reservations) and hazard H4 (counting quantity)."""

from __future__ import annotations

from decimal import Decimal

import pytest
from decision_fixtures import fill_window, fired_market

from arc.decision.quota import QuotaLedger, QuotaSnapshot
from arc.domain.enums import Direction
from arc.domain.models import MarketInstance

MINIMUM = Decimal("5")


@pytest.fixture
def ledger() -> QuotaLedger:
    return QuotaLedger(max_trades_per_market=3, min_tradable_size=MINIMUM)


@pytest.fixture
def market() -> MarketInstance:
    return fired_market(fired=(15, 10, 7, 5, 3))


class TestConstruction:
    @pytest.mark.parametrize("limit", [0, -1])
    def test_a_non_positive_limit_is_refused(self, limit: int) -> None:
        """A limit of zero would silently disable trading for a reason that reads as
        a working quota rather than as a misconfiguration."""
        with pytest.raises(ValueError, match="at least 1"):
            QuotaLedger(max_trades_per_market=limit, min_tradable_size=MINIMUM)

    @pytest.mark.parametrize("minimum", [Decimal("0"), Decimal("-1")])
    def test_a_non_positive_minimum_is_refused(self, minimum: Decimal) -> None:
        """With a minimum of zero, every window with any fill at all would count,
        including a one-share dust fill."""
        with pytest.raises(ValueError, match="positive"):
            QuotaLedger(max_trades_per_market=3, min_tradable_size=minimum)


class TestH4CountingQuantityNotOrders:
    def test_a_window_with_no_fills_does_not_count(
        self, ledger: QuotaLedger, market: MarketInstance
    ) -> None:
        assert not ledger.counts(market, 3)
        assert ledger.used(market) == 0

    def test_a_sub_minimum_fill_does_not_count(
        self, ledger: QuotaLedger, market: MarketInstance
    ) -> None:
        fill_window(market, 3, size=Decimal("4"))
        assert not ledger.counts(market, 3)
        assert ledger.used(market) == 0

    def test_exactly_the_minimum_counts(
        self, ledger: QuotaLedger, market: MarketInstance
    ) -> None:
        fill_window(market, 3, size=MINIMUM)
        assert ledger.counts(market, 3)
        assert ledger.used(market) == 1

    def test_a_reprice_chain_is_summed_not_counted(
        self, ledger: QuotaLedger, market: MarketInstance
    ) -> None:
        """A reprice is cancel-then-place, so one logical position produces several
        order ids. Counting orders would let three sub-minimum fills consume three
        trades of budget."""
        fill_window(market, 3, size=Decimal("2"), order_suffix="a")
        fill_window(market, 3, size=Decimal("2"), order_suffix="b")
        assert not ledger.counts(market, 3)
        fill_window(market, 3, size=Decimal("1"), order_suffix="c")
        assert ledger.counts(market, 3)
        # Three orders, one trade of budget.
        assert len([o for o in market.orders if o.offset_seconds == 3]) == 3
        assert ledger.used(market) == 1

    def test_fills_on_one_window_do_not_count_toward_another(
        self, ledger: QuotaLedger, market: MarketInstance
    ) -> None:
        fill_window(market, 3, size=Decimal("9"))
        assert ledger.counts(market, 3)
        assert not ledger.counts(market, 5)

    def test_used_recounts_from_the_fills_every_call(
        self, ledger: QuotaLedger, market: MarketInstance
    ) -> None:
        """Not a stored counter. A redelivered fill or a missed increment would put a
        counter permanently out of step with the fills on disk, with nothing to
        detect the divergence."""
        assert ledger.used(market) == 0
        fill_window(market, 3, size=MINIMUM)
        assert ledger.used(market) == 1
        fill_window(market, 5, size=MINIMUM)
        assert ledger.used(market) == 2


class TestH2Reservations:
    def test_reserving_makes_a_slot_unavailable_before_any_fill(
        self, ledger: QuotaLedger, market: MarketInstance
    ) -> None:
        """The whole point: three windows must not each pass a used-only check inside
        one second and open four positions against a three-trade budget."""
        for offset in (3, 5, 7):
            ledger.reserve(market, offset)
        snapshot = ledger.snapshot(market)
        assert snapshot.used == 0
        assert snapshot.reserved == 3
        assert snapshot.exhausted

    def test_reserving_is_idempotent(
        self, ledger: QuotaLedger, market: MarketInstance
    ) -> None:
        """A duplicated decision pass must not consume two slots for one window."""
        assert ledger.reserve(market, 3)
        assert not ledger.reserve(market, 3)
        assert ledger.snapshot(market).reserved == 1

    def test_releasing_returns_the_slot(
        self, ledger: QuotaLedger, market: MarketInstance
    ) -> None:
        ledger.reserve(market, 3)
        assert ledger.release(market, 3)
        assert ledger.snapshot(market).reserved == 0

    def test_releasing_an_unreserved_window_reports_false(
        self, ledger: QuotaLedger, market: MarketInstance
    ) -> None:
        assert not ledger.release(market, 3)

    def test_a_filled_window_is_counted_once_not_twice(
        self, ledger: QuotaLedger, market: MarketInstance
    ) -> None:
        """A reservation that stayed in the total after the fill would double-charge
        the budget for a single trade."""
        ledger.reserve(market, 3)
        fill_window(market, 3, size=MINIMUM)
        snapshot = ledger.snapshot(market)
        assert snapshot.used == 1
        assert snapshot.reserved == 0
        assert snapshot.committed == 1

    def test_a_reservation_on_a_sub_minimum_fill_still_holds_the_slot(
        self, ledger: QuotaLedger, market: MarketInstance
    ) -> None:
        """The order is live with partial fill below the minimum. The slot is
        committed, so releasing it here would let a second trade open against the
        same budget while the first is still resting on the book."""
        ledger.reserve(market, 3)
        fill_window(market, 3, size=Decimal("2"))
        snapshot = ledger.snapshot(market)
        assert snapshot.used == 0
        assert snapshot.reserved == 1


class TestTheSnapshotArithmetic:
    def test_available_is_the_limit_less_everything_committed(self) -> None:
        assert QuotaSnapshot(used=1, reserved=1, limit=3).available == 1

    def test_available_never_goes_negative(self) -> None:
        """A negative would read as "more budget than configured" wherever it is
        formatted or compared with >."""
        assert QuotaSnapshot(used=4, reserved=2, limit=3).available == 0

    def test_exhausted_is_exactly_no_availability(self) -> None:
        assert QuotaSnapshot(used=3, reserved=0, limit=3).exhausted
        assert not QuotaSnapshot(used=2, reserved=0, limit=3).exhausted


class TestPerMarketIsolation:
    def test_reservations_live_on_the_instance(self, ledger: QuotaLedger) -> None:
        """A11/H2. A reservation that outlived its market would consume the next
        market's quota and read as a correctly-enforced limit."""
        first = fired_market(window_ts=1754400000)
        second = fired_market(window_ts=1754400300)
        ledger.reserve(first, 3)
        assert ledger.snapshot(first).reserved == 1
        assert ledger.snapshot(second).reserved == 0

    def test_one_ledger_serves_both_markets_alive_at_a_boundary(
        self, ledger: QuotaLedger
    ) -> None:
        """D6: at most two MarketInstances are live, and both are served correctly
        because the ledger holds only configuration."""
        closing = fired_market(window_ts=1754400000)
        current = fired_market(window_ts=1754400300)
        fill_window(closing, 3, size=MINIMUM)
        fill_window(closing, 5, size=MINIMUM)
        fill_window(current, 3, size=MINIMUM)
        assert ledger.used(closing) == 2
        assert ledger.used(current) == 1

    def test_the_ledger_stores_nothing_per_market(self) -> None:
        assert set(QuotaLedger.__slots__) == {"_max_trades", "_min_tradable_size"}

    def test_a_fresh_market_starts_with_a_clean_quota(self, ledger: QuotaLedger) -> None:
        """No reset() anywhere: a new market is a NEW OBJECT (A11)."""
        market = fired_market()
        assert not hasattr(market, "reset")
        assert ledger.snapshot(market) == QuotaSnapshot(used=0, reserved=0, limit=3)


class TestDirectionsHeldFeedsTheOpposingGate:
    def test_only_filled_quantity_counts_as_held(self) -> None:
        """An unfilled order is not a position. Treating it as one would block the
        opposite side for a trade that never happened."""
        market = fired_market(direction=Direction.UP)
        assert market.directions_held() == frozenset()
        fill_window(market, 3, size=MINIMUM)
        assert market.directions_held() == frozenset({Direction.UP})
