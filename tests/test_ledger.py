"""Unified Ledger: every window appears, records update in place, Decimals are strings.

The failures pinned here are the ones that would silently lose history: a window
that produced no order vanishing from the only history page, a reprice chain being
counted as three trades, and a Decimal crossing the API boundary as a float.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from arc.domain.enums import Direction, OrderState, Outcome, WindowState
from arc.domain.models import ExecutionWindow, Fill, MarketInstance, Order, Settlement
from arc.runtime.ledger import (
    BUFFER_NOT_SATISFIED,
    ledger_records,
    ledger_totals,
    search_records,
)
from arc.storage.store import Store

_NOW = 1_760_000_000.0
_WINDOW_TS = 1_760_000_100


@pytest.fixture
def store(tmp_path: object) -> Store:
    db = Store(f"{tmp_path}/arc.db")  # type: ignore[str-bytes-safe]
    db.migrate(_NOW)
    return db


def _market(store: Store, window_ts: int = _WINDOW_TS) -> str:
    # MarketInstance.create rather than a bare constructor: the window rows only
    # exist because create() populates market.windows, and create_market inserts them.
    market = MarketInstance.create(window_ts, (5,))
    store.create_market(market, _NOW)
    store.save_ptb(market.slug, Decimal("120000.00"), _NOW)
    return market.slug


def _frozen(store: Store, slug: str, offset: int) -> None:
    store.save_window_frozen(
        slug,
        ExecutionWindow(
            offset_seconds=offset,
            state=WindowState.FROZEN,
            opening_twap=Decimal("120050.00"),
            ptb=Decimal("120000.00"),
            buffer=Decimal("35.000000"),
            direction=Direction.UP,
            locked_trigger=Decimal("120035.000000"),
            frozen_at=_NOW,
        ),
        _NOW,
    )


def _order(
    store: Store,
    slug: str,
    offset: int,
    order_id: str,
    *,
    state: OrderState = OrderState.SUBMITTED,
    size: str = "80",
    at: float = _NOW,
) -> Order:
    order = Order(
        order_id=order_id,
        market_slug=slug,
        offset_seconds=offset,
        direction=Direction.UP,
        price=Decimal("0.74"),
        size=Decimal(size),
        state=state,
        created_at=at,
        updated_at=at,
        venue_order_id=f"v-{order_id}",
        reprice_chain_id="chain-1",
    )
    store.save_order(order)
    return order


class TestEveryWindowIsARecord:
    def test_a_window_with_no_order_still_appears(self, store: Store) -> None:
        """BUFFER_NOT_SATISFIED is the most important non-event of the day."""
        slug = _market(store)
        _frozen(store, slug, 5)
        store.save_window_state(slug, 5, WindowState.EXPIRED)
        (record,) = ledger_records(store)
        assert record.local_order_id == ""
        assert record.buffer_status == BUFFER_NOT_SATISFIED
        assert record.rejection_reason == BUFFER_NOT_SATISFIED

    def test_no_direction_is_shown_never_inferred(self, store: Store) -> None:
        """A NO_DIRECTION window carries no frozen values; the record must still say so."""
        slug = _market(store)
        store.save_window_state(slug, 5, WindowState.NO_DIRECTION)
        (record,) = ledger_records(store)
        assert record.direction == WindowState.NO_DIRECTION.value
        assert record.buffer_status == "NO_DIRECTION"

    def test_frozen_values_are_reported_from_the_window_row(self, store: Store) -> None:
        slug = _market(store)
        _frozen(store, slug, 5)
        (record,) = ledger_records(store)
        assert record.ptb == Decimal("120000.00")
        assert record.locked_trigger == Decimal("120035.000000")
        assert record.buffer == Decimal("35.000000")
        assert record.signal_twap == Decimal("120050.00")


class TestOneRecordPerWindowNotPerOrder:
    def test_a_reprice_chain_is_one_record(self, store: Store) -> None:
        """Keying by order would show one window as three trades."""
        slug = _market(store)
        _frozen(store, slug, 5)
        _order(store, slug, 5, "o1", state=OrderState.CANCELLED, at=_NOW)
        _order(store, slug, 5, "o2", state=OrderState.SUBMITTED, at=_NOW + 1)
        (record,) = ledger_records(store)
        assert record.local_order_id == "o2"
        assert record.state == OrderState.SUBMITTED.value
        assert record.notes == "reprice chain of 2"

    def test_filled_quantity_sums_across_the_chain(self, store: Store) -> None:
        """A partial fill on a cancelled leg is still a real position (hazard H4)."""
        slug = _market(store)
        _frozen(store, slug, 5)
        _order(store, slug, 5, "o1", state=OrderState.CANCELLED, at=_NOW)
        _order(store, slug, 5, "o2", state=OrderState.PARTIAL, at=_NOW + 1)
        store.save_fill(
            Fill(fill_id="f1", order_id="o1", market_slug=slug, size=Decimal("20"),
                 price=Decimal("0.74"), ts=_NOW + 0.5)
        )
        store.save_fill(
            Fill(fill_id="f2", order_id="o2", market_slug=slug, size=Decimal("30"),
                 price=Decimal("0.76"), ts=_NOW + 2)
        )
        (record,) = ledger_records(store)
        assert record.filled_quantity == Decimal("50")
        assert record.remaining_quantity == Decimal("30")
        # Size weighted, not the mean of the prices.
        assert record.fill_price == Decimal("0.752")
        assert record.fill_time == _NOW + 2


class TestRecordsUpdateInPlace:
    def test_state_change_does_not_add_a_row(self, store: Store) -> None:
        slug = _market(store)
        _frozen(store, slug, 5)
        _order(store, slug, 5, "o1")
        assert len(ledger_records(store)) == 1
        store.save_order_state("o1", OrderState.FILLED, _NOW + 3)
        records = ledger_records(store)
        assert len(records) == 1
        assert records[0].state == OrderState.FILLED.value


class TestRejectionReasonIsSeparateFromState:
    def test_reason_survives_on_the_record(self, store: Store) -> None:
        slug = _market(store)
        _frozen(store, slug, 5)
        _order(store, slug, 5, "o1")
        store.save_order_state(
            "o1", OrderState.REJECTED, _NOW + 1, rejection_reason="POST_ONLY_WOULD_CROSS"
        )
        (record,) = ledger_records(store)
        assert record.state == OrderState.REJECTED.value
        assert record.rejection_reason == "POST_ONLY_WOULD_CROSS"
        assert record.rejection_display


class TestSerialization:
    def test_every_money_value_is_a_string(self, store: Store) -> None:
        """A JSON number loses precision and the loss is invisible in the UI."""
        slug = _market(store)
        _frozen(store, slug, 5)
        _order(store, slug, 5, "o1")
        payload = ledger_records(store)[0].as_json()
        for key in (
            "ptb", "signal_twap", "locked_trigger", "buffer", "order_price",
            "quantity", "filled_quantity",
        ):
            assert payload[key] is None or isinstance(payload[key], str), key


class TestSearchAndTotals:
    def test_search_matches_venue_order_id(self, store: Store) -> None:
        slug = _market(store)
        _frozen(store, slug, 5)
        _order(store, slug, 5, "o1")
        records = ledger_records(store)
        assert search_records(records, "v-o1")
        assert not search_records(records, "nothing-like-this")

    def test_settlement_result_and_totals(self, store: Store) -> None:
        slug = _market(store)
        _frozen(store, slug, 5)
        _order(store, slug, 5, "o1", state=OrderState.FILLED)
        store.save_settlement(
            Settlement(
                market_slug=slug,
                outcome=Outcome.UP,
                settlement_twap=Decimal("120060.00"),
                ptb=Decimal("120000.00"),
                settled_at=_NOW + 300,
                pnl=Decimal("12.50"),
            )
        )
        (record,) = ledger_records(store)
        assert "WIN" in record.settlement_result
        assert record.pnl == Decimal("12.50")
        totals = ledger_totals((record,))
        assert totals == {
            "markets_processed": 1,
            "filled_orders": 1,
            "rejected_orders": 0,
            "buffer_not_satisfied": 0,
            "win_count": 1,
            "loss_count": 0,
            "average_fill_seconds": None,
        }
