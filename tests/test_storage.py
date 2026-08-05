"""The Store: the only component allowed to touch SQLite.

Every guarantee tested here is a database-level guarantee, not a Python-level
one, because the whole point of routing every write through this one file is
that a crash between a decision and its consequence must still leave the
correct state on disk. `WHERE ptb IS NULL`, `UNIQUE(market_slug,
offset_seconds)`, and `INSERT OR IGNORE` on a venue-issued id are the actual
enforcement mechanisms; an in-memory check beside them would only be a second
opinion that a restart can't consult.
"""

from __future__ import annotations

import sqlite3
from decimal import Decimal
from pathlib import Path

import pytest
from conftest import CLOSE_TS, OFFSETS, WINDOW_TS

from arc.domain.enums import Direction, MarketPhase, OrderState, Outcome, WindowState
from arc.domain.models import (
    ExecutionIntent,
    Fill,
    MarketInstance,
    Observation,
    Order,
    Settlement,
)
from arc.errors import SchemaMigrationError, StorageError
from arc.storage.schema import EXPECTED_TABLES, FORBIDDEN_TABLES, SCHEMA_VERSION
from arc.storage.store import Store


def _market() -> MarketInstance:
    return MarketInstance.create(window_ts=WINDOW_TS, offsets=OFFSETS)


class TestMigrationBringsTheSchemaUp:
    def test_fresh_database_starts_at_version_zero(self, tmp_path: Path) -> None:
        store = Store(tmp_path / "arc.db")
        assert store.schema_version() == 0
        store.close()

    def test_migrate_reaches_the_expected_version(self, tmp_path: Path) -> None:
        store = Store(tmp_path / "arc.db")
        result = store.migrate(1.0)
        assert result == SCHEMA_VERSION
        assert store.schema_version() == SCHEMA_VERSION
        store.close()

    def test_migrate_is_idempotent(self, tmp_path: Path) -> None:
        """Running migrate twice must not error or reapply anything."""
        store = Store(tmp_path / "arc.db")
        store.migrate(1.0)
        again = store.migrate(2.0)
        assert again == SCHEMA_VERSION
        rows = store.connection.execute("SELECT COUNT(*) AS n FROM schema_migrations").fetchone()
        assert rows["n"] == SCHEMA_VERSION
        store.close()

    def test_expected_tables_all_exist_after_migration(self, store: Store) -> None:
        names = set(store.table_names())
        for table in EXPECTED_TABLES:
            assert table in names

    def test_no_forbidden_event_sourcing_tables_exist(self, store: Store) -> None:
        """A3/A4: event sourcing was removed as an architecture, not just unused."""
        names = set(store.table_names())
        for table in FORBIDDEN_TABLES:
            assert table not in names

    def test_a_database_claiming_a_newer_schema_is_refused(self, tmp_path: Path) -> None:
        """Running an old build against a newer database must fail loudly.

        Continuing anyway risks writing with a schema the running code does not
        understand, which is exactly the kind of half-applied state the write-
        before-act guarantee depends on never happening.
        """
        db_path = tmp_path / "arc.db"
        store = Store(db_path)
        store.migrate(1.0)
        store.connection.execute(
            "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
            (SCHEMA_VERSION + 1, 1.0),
        )
        store.connection.commit()
        store.close()

        reopened = Store(db_path)
        with pytest.raises(SchemaMigrationError, match="newer than this build"):
            reopened.migrate(2.0)
        reopened.close()

    def test_integrity_check_passes_on_a_fresh_database(self, store: Store) -> None:
        assert store.integrity_check() == "ok"

    def test_expected_schema_version_matches_the_module_constant(self, store: Store) -> None:
        assert store.expected_schema_version() == SCHEMA_VERSION


class TestPragmasAreTheDurabilityGuarantee:
    def test_wal_mode_is_active(self, store: Store) -> None:
        mode = store.connection.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode.lower() == "wal"

    def test_synchronous_is_full(self, store: Store) -> None:
        """NORMAL can lose the last WAL transactions on power loss (A4)."""
        level = store.connection.execute("PRAGMA synchronous").fetchone()[0]
        assert level == 2  # sqlite reports FULL as 2

    def test_foreign_keys_are_enforced(self, store: Store) -> None:
        level = store.connection.execute("PRAGMA foreign_keys").fetchone()[0]
        assert level == 1

    def test_an_orphan_fill_is_rejected_by_the_database(self, store: Store) -> None:
        with pytest.raises(sqlite3.IntegrityError):
            store.connection.execute(
                "INSERT INTO fills (fill_id, order_id, market_slug, size, price, ts) "
                "VALUES ('f1', 'no-such-order', 'no-such-market', '1', '1', 1.0)"
            )


class TestMoneyColumnsRoundTripExactly:
    """Every money column is TEXT so a Decimal round-trips exactly (schema.py)."""

    def test_ptb_round_trips_through_text(self, store: Store) -> None:
        market = _market()
        store.create_market(market, 1.0)
        store.save_ptb(market.slug, Decimal("120000.789"), 1.0)
        loaded = store.load_ptb(market.slug)
        assert loaded == Decimal("120000.789")

    def test_the_stored_column_is_actually_text_not_a_float(self, store: Store) -> None:
        market = _market()
        store.create_market(market, 1.0)
        store.save_ptb(market.slug, Decimal("0.1"), 1.0)
        row = store.connection.execute(
            "SELECT ptb FROM markets WHERE slug = ?", (market.slug,)
        ).fetchone()
        assert row["ptb"] == "0.1"  # not "0.1000000000000000055511151231257827"

    def test_running_sum_round_trips_a_long_decimal(self, store: Store) -> None:
        market = _market()
        store.create_market(market, 1.0)
        store.save_accumulator(market.slug, Decimal("100300.123456"), 3, 1.0)
        row = store.load_market_row(market.slug)
        assert row is not None
        assert Decimal(row["running_sum"]) == Decimal("100300.123456")


class TestPtbIsWrittenOnce:
    """WHERE ptb IS NULL is the guarantee, not the Python-level bool return."""

    def test_first_write_succeeds(self, store: Store) -> None:
        market = _market()
        store.create_market(market, 1.0)
        assert store.save_ptb(market.slug, Decimal("100"), 1.0) is True
        assert store.load_ptb(market.slug) == Decimal("100")

    def test_second_write_is_refused_even_with_the_same_value(self, store: Store) -> None:
        market = _market()
        store.create_market(market, 1.0)
        store.save_ptb(market.slug, Decimal("100"), 1.0)
        assert store.save_ptb(market.slug, Decimal("100"), 2.0) is False
        assert store.load_ptb(market.slug) == Decimal("100")

    def test_second_write_with_a_different_value_does_not_win(self, store: Store) -> None:
        """The exact scenario the WHERE clause exists to prevent."""
        market = _market()
        store.create_market(market, 1.0)
        store.save_ptb(market.slug, Decimal("100"), 1.0)
        store.save_ptb(market.slug, Decimal("999"), 2.0)
        assert store.load_ptb(market.slug) == Decimal("100")

    def test_load_ptb_is_none_before_any_write(self, store: Store) -> None:
        market = _market()
        store.create_market(market, 1.0)
        assert store.load_ptb(market.slug) is None

    def test_ptb_frozen_at_is_recorded_on_first_write_only(self, store: Store) -> None:
        market = _market()
        store.create_market(market, 1.0)
        store.save_ptb(market.slug, Decimal("100"), 5.0)
        store.save_ptb(market.slug, Decimal("999"), 9.0)
        row = store.load_market_row(market.slug)
        assert row is not None
        assert row["ptb_frozen_at"] == 5.0


class TestCreateMarketDoesNotResetAnExistingOne:
    """INSERT OR IGNORE: rediscovering a market underway must not blank its state."""

    def test_first_create_returns_true(self, store: Store) -> None:
        assert store.create_market(_market(), 1.0) is True

    def test_second_create_of_the_same_slug_returns_false(self, store: Store) -> None:
        market = _market()
        store.create_market(market, 1.0)
        assert store.create_market(market, 2.0) is False

    def test_a_second_create_does_not_reset_a_frozen_ptb(self, store: Store) -> None:
        market = _market()
        store.create_market(market, 1.0)
        store.save_ptb(market.slug, Decimal("100"), 1.0)
        store.save_phase(market.slug, MarketPhase.ACTIVE, 2.0)
        store.create_market(market, 3.0)  # rediscovery
        assert store.load_ptb(market.slug) == Decimal("100")
        row = store.load_market_row(market.slug)
        assert row is not None
        assert row["phase"] == MarketPhase.ACTIVE.value

    def test_create_market_writes_all_configured_windows(self, store: Store) -> None:
        market = _market()
        store.create_market(market, 1.0)
        rows = store.windows_for(market.slug)
        assert {r["offset_seconds"] for r in rows} == set(OFFSETS)
        assert all(r["state"] == WindowState.PENDING.value for r in rows)


class TestSaveWindowFrozenRefusesAPartialFreeze:
    """A row with a real opening_twap and a null trigger must never be written."""

    def test_a_fully_frozen_window_is_written(self, store: Store) -> None:
        market = _market()
        store.create_market(market, 1.0)
        window = market.window(3)
        window.freeze(
            opening_twap=Decimal("100"), ptb=Decimal("99"), buffer=Decimal("1"), frozen_at=1.0
        )
        assert store.save_window_frozen(market.slug, window, 1.0) is True
        row = store.windows_for(market.slug)
        frozen_row = next(r for r in row if r["offset_seconds"] == 3)
        assert frozen_row["state"] == WindowState.FROZEN.value
        assert Decimal(frozen_row["locked_trigger"]) == Decimal("101")

    def test_an_unfrozen_window_is_refused(self, store: Store) -> None:
        market = _market()
        store.create_market(market, 1.0)
        window = market.window(3)  # never frozen
        with pytest.raises(StorageError, match="partially frozen"):
            store.save_window_frozen(market.slug, window, 1.0)

    def test_refusal_leaves_the_stored_row_untouched(self, store: Store) -> None:
        """A failed save must not leave a half-written row behind either."""
        market = _market()
        store.create_market(market, 1.0)
        window = market.window(3)
        with pytest.raises(StorageError):
            store.save_window_frozen(market.slug, window, 1.0)
        row = next(r for r in store.windows_for(market.slug) if r["offset_seconds"] == 3)
        assert row["state"] == WindowState.PENDING.value
        assert row["locked_trigger"] is None


class TestRestoreFrozenIsVerbatim:
    """A4: reload direction and locked_trigger as stored, with no recomputation."""

    def test_restore_returns_none_when_never_frozen(self, store: Store) -> None:
        market = _market()
        store.create_market(market, 1.0)
        assert store.restore_frozen(market.slug, 3) is None

    def test_restore_reproduces_the_exact_frozen_values(self, store: Store) -> None:
        market = _market()
        store.create_market(market, 1.0)
        window = market.window(5)
        window.freeze(
            opening_twap=Decimal("99.50"), ptb=Decimal("100.00"), buffer=Decimal("1.25"),
            frozen_at=42.0,
        )
        store.save_window_frozen(market.slug, window, 42.0)

        restored = store.restore_frozen(market.slug, 5)
        assert restored is not None
        assert restored["direction"] is Direction.DOWN
        assert restored["locked_trigger"] == Decimal("98.25")
        assert restored["opening_twap"] == Decimal("99.50")
        assert restored["ptb"] == Decimal("100.00")
        assert restored["buffer"] == Decimal("1.25")
        assert restored["frozen_at"] == 42.0

    def test_restore_does_not_touch_a_sibling_windows_row(self, store: Store) -> None:
        market = _market()
        store.create_market(market, 1.0)
        window = market.window(3)
        window.freeze(
            opening_twap=Decimal("100"), ptb=Decimal("99"), buffer=Decimal("1"), frozen_at=1.0
        )
        store.save_window_frozen(market.slug, window, 1.0)
        assert store.restore_frozen(market.slug, 15) is None


class TestIntentUniquenessIsArbitratedByTheDatabase:
    """UNIQUE(market_slug, offset_seconds): exactly one intent per window, ever."""

    def _intent(self, slug: str, offset: int = 3) -> ExecutionIntent:
        return ExecutionIntent(
            market_slug=slug, offset_seconds=offset, direction=Direction.UP,
            signal_twap=Decimal("100"), locked_trigger=Decimal("99"), created_at=1.0,
        )

    def test_first_intent_is_recorded(self, store: Store) -> None:
        market = _market()
        store.create_market(market, 1.0)
        assert store.save_intent(self._intent(market.slug)) is True
        assert store.has_intent(market.slug, 3) is True

    def test_a_second_intent_for_the_same_window_is_refused(self, store: Store) -> None:
        market = _market()
        store.create_market(market, 1.0)
        store.save_intent(self._intent(market.slug))
        second = ExecutionIntent(
            market_slug=market.slug, offset_seconds=3, direction=Direction.DOWN,
            signal_twap=Decimal("50"), locked_trigger=Decimal("49"), created_at=2.0,
            intent_id="different-id",
        )
        assert store.save_intent(second) is False
        stored = store.intents_for(market.slug)
        assert len(stored) == 1
        assert stored[0].direction is Direction.UP  # the first one won, unchanged

    def test_different_offsets_on_the_same_market_are_independent(self, store: Store) -> None:
        market = _market()
        store.create_market(market, 1.0)
        assert store.save_intent(self._intent(market.slug, 3)) is True
        assert store.save_intent(self._intent(market.slug, 5)) is True
        assert len(store.intents_for(market.slug)) == 2

    def test_has_intent_is_false_when_none_recorded(self, store: Store) -> None:
        market = _market()
        store.create_market(market, 1.0)
        assert store.has_intent(market.slug, 3) is False


class TestFillIdempotencyOnRedelivery:
    """INSERT OR IGNORE on fill_id: a websocket redelivery must not double-count."""

    def _order(self, slug: str) -> Order:
        return Order(
            order_id="o1", market_slug=slug, offset_seconds=3, direction=Direction.UP,
            price=Decimal("0.5"), size=Decimal("100"),
        )

    def test_first_fill_is_recorded(self, store: Store) -> None:
        market = _market()
        store.create_market(market, 1.0)
        store.save_order(self._order(market.slug))
        fill = Fill(fill_id="f1", order_id="o1", market_slug=market.slug,
                    size=Decimal("10"), price=Decimal("0.5"), ts=1.0)
        assert store.save_fill(fill) is True
        assert len(store.fills_for(market.slug)) == 1

    def test_the_same_fill_id_redelivered_is_ignored(self, store: Store) -> None:
        market = _market()
        store.create_market(market, 1.0)
        store.save_order(self._order(market.slug))
        fill = Fill(fill_id="f1", order_id="o1", market_slug=market.slug,
                    size=Decimal("10"), price=Decimal("0.5"), ts=1.0)
        store.save_fill(fill)
        assert store.save_fill(fill) is False
        assert len(store.fills_for(market.slug)) == 1

    def test_redelivery_does_not_double_the_filled_size(self, store: Store) -> None:
        market = _market()
        store.create_market(market, 1.0)
        store.save_order(self._order(market.slug))
        fill = Fill(fill_id="f1", order_id="o1", market_slug=market.slug,
                    size=Decimal("10"), price=Decimal("0.5"), ts=1.0)
        store.save_fill(fill)
        store.save_fill(fill)
        store.save_fill(fill)
        assert store.filled_size_for_window(market.slug, 3) == Decimal("10")


class TestFilledSizeForWindowSumsTheRepriceChain:
    """Hazard H4: summed across every order id for the offset, not one order."""

    def _chained_orders(self, slug: str) -> list[Order]:
        return [
            Order(order_id=f"o{i}", market_slug=slug, offset_seconds=3,
                  direction=Direction.UP, price=Decimal(p), size=Decimal("100"),
                  reprice_chain_id="chain-1")
            for i, p in enumerate(("0.74", "0.75", "0.76"))
        ]

    def test_sums_across_multiple_orders_for_the_same_offset(self, store: Store) -> None:
        market = _market()
        store.create_market(market, 1.0)
        for order in self._chained_orders(market.slug):
            store.save_order(order)
        for i, size in enumerate(("2", "3", "4")):
            store.save_fill(Fill(
                fill_id=f"f{i}", order_id=f"o{i}", market_slug=market.slug,
                size=Decimal(size), price=Decimal("0.75"), ts=float(i),
            ))
        assert store.filled_size_for_window(market.slug, 3) == Decimal("9")

    def test_a_different_offsets_fills_are_excluded(self, store: Store) -> None:
        market = _market()
        store.create_market(market, 1.0)
        store.save_order(Order(
            order_id="o-other", market_slug=market.slug, offset_seconds=5,
            direction=Direction.UP, price=Decimal("0.5"), size=Decimal("100"),
        ))
        store.save_fill(Fill(
            fill_id="f-other", order_id="o-other", market_slug=market.slug,
            size=Decimal("50"), price=Decimal("0.5"), ts=1.0,
        ))
        assert store.filled_size_for_window(market.slug, 3) == Decimal("0")

    def test_no_orders_at_all_is_zero_not_an_error(self, store: Store) -> None:
        market = _market()
        store.create_market(market, 1.0)
        assert store.filled_size_for_window(market.slug, 3) == Decimal("0")


class TestLiveOrdersIncludesIndeterminate:
    """A13: an unacknowledged cancel might still be live; omitting it here would
    let the cancellation sweep skip a resting order into settlement.
    """

    def _order(self, slug: str, order_id: str, state: OrderState) -> Order:
        return Order(
            order_id=order_id, market_slug=slug, offset_seconds=3,
            direction=Direction.UP, price=Decimal("0.5"), size=Decimal("1"), state=state,
        )

    def test_live_orders_includes_indeterminate(self, store: Store) -> None:
        market = _market()
        store.create_market(market, 1.0)
        store.save_order(self._order(market.slug, "a", OrderState.SUBMITTED))
        store.save_order(self._order(market.slug, "b", OrderState.FILLED))
        store.save_order(self._order(market.slug, "c", OrderState.INDETERMINATE))
        ids = {o.order_id for o in store.live_orders(market.slug)}
        assert ids == {"a", "c"}

    def test_live_orders_without_a_slug_filters_across_markets(self, store: Store) -> None:
        m1, m2 = _market(), MarketInstance.create(window_ts=CLOSE_TS, offsets=OFFSETS)
        store.create_market(m1, 1.0)
        store.create_market(m2, 1.0)
        store.save_order(self._order(m1.slug, "a", OrderState.SUBMITTED))
        store.save_order(self._order(m2.slug, "b", OrderState.PENDING))
        store.save_order(self._order(m2.slug, "c", OrderState.FILLED))
        ids = {o.order_id for o in store.live_orders()}
        assert ids == {"a", "b"}

    def test_updating_order_state_moves_it_out_of_live(self, store: Store) -> None:
        market = _market()
        store.create_market(market, 1.0)
        store.save_order(self._order(market.slug, "a", OrderState.SUBMITTED))
        store.save_order_state("a", OrderState.FILLED, 2.0)
        assert store.live_orders(market.slug) == ()


class TestPruneObservationsLeavesAggregatesIntact:
    """Only raw ticks are pruned; running_sum/observation_count/settlement_twap
    live on the markets row and must not move when old ticks are deleted.
    """

    def test_prune_deletes_only_ticks_older_than_the_horizon(self, store: Store) -> None:
        market = _market()
        store.create_market(market, 1.0)
        store.save_observation(market.slug, Observation(ts=1.0, price=Decimal("100")), 1.0)
        store.save_observation(market.slug, Observation(ts=100.0, price=Decimal("101")), 100.0)
        deleted = store.prune_observations(50.0)
        assert deleted == 1
        remaining = store.observations_for(market.slug)
        assert len(remaining) == 1
        assert remaining[0].ts == 100.0

    def test_prune_does_not_touch_the_markets_row_aggregates(self, store: Store) -> None:
        market = _market()
        store.create_market(market, 1.0)
        store.save_accumulator(market.slug, Decimal("201"), 2, 1.0)
        store.save_settlement_twap(market.slug, Decimal("100.5"), 1.0)
        store.save_observation(market.slug, Observation(ts=1.0, price=Decimal("100")), 1.0)

        store.prune_observations(999999.0)  # prunes everything

        row = store.load_market_row(market.slug)
        assert row is not None
        assert Decimal(row["running_sum"]) == Decimal("201")
        assert row["observation_count"] == 2
        assert Decimal(row["settlement_twap"]) == Decimal("100.5")
        assert store.observation_count(market.slug) == 0

    def test_prune_returns_zero_when_nothing_is_old_enough(self, store: Store) -> None:
        market = _market()
        store.create_market(market, 1.0)
        store.save_observation(market.slug, Observation(ts=100.0, price=Decimal("100")), 100.0)
        assert store.prune_observations(1.0) == 0


class TestObservationsBetweenIsHalfOpen:
    """[start, end): an observation exactly at close belongs to one window only."""

    def test_end_boundary_is_excluded(self, store: Store) -> None:
        market = _market()
        store.create_market(market, 1.0)
        store.save_observation(market.slug, Observation(ts=100.0, price=Decimal("1")), 100.0)
        assert store.observations_between(0.0, 100.0) == ()

    def test_start_boundary_is_included(self, store: Store) -> None:
        market = _market()
        store.create_market(market, 1.0)
        store.save_observation(market.slug, Observation(ts=100.0, price=Decimal("1")), 100.0)
        result = store.observations_between(100.0, 200.0)
        assert len(result) == 1

    def test_an_observation_never_appears_in_two_adjacent_ranges(self, store: Store) -> None:
        market = _market()
        store.create_market(market, 1.0)
        store.save_observation(market.slug, Observation(ts=300.0, price=Decimal("1")), 300.0)
        earlier = store.observations_between(0.0, 300.0)
        later = store.observations_between(300.0, 600.0)
        assert earlier == ()
        assert len(later) == 1


class TestSettlementIsRecordedOnceFromTheVenue:
    def test_first_settlement_is_recorded(self, store: Store) -> None:
        market = _market()
        store.create_market(market, 1.0)
        settlement = Settlement(
            market_slug=market.slug, outcome=Outcome.UP, settlement_twap=Decimal("100.5"),
            ptb=Decimal("100"), settled_at=1.0,
        )
        assert store.save_settlement(settlement) is True
        loaded = store.settlement_for(market.slug)
        assert loaded is not None
        assert loaded.outcome is Outcome.UP

    def test_a_second_settlement_for_the_same_market_is_refused(self, store: Store) -> None:
        market = _market()
        store.create_market(market, 1.0)
        first = Settlement(
            market_slug=market.slug, outcome=Outcome.UP, settlement_twap=Decimal("100.5"),
            ptb=Decimal("100"), settled_at=1.0,
        )
        store.save_settlement(first)
        contradicting = Settlement(
            market_slug=market.slug, outcome=Outcome.DOWN, settlement_twap=Decimal("99"),
            ptb=Decimal("100"), settled_at=2.0,
        )
        assert store.save_settlement(contradicting) is False
        loaded = store.settlement_for(market.slug)
        assert loaded is not None
        assert loaded.outcome is Outcome.UP

    def test_settlement_for_an_unsettled_market_is_none(self, store: Store) -> None:
        market = _market()
        store.create_market(market, 1.0)
        assert store.settlement_for(market.slug) is None

    def test_divergence_logged_round_trips_as_a_bool(self, store: Store) -> None:
        market = _market()
        store.create_market(market, 1.0)
        settlement = Settlement(
            market_slug=market.slug, outcome=Outcome.DOWN, settlement_twap=Decimal("99"),
            ptb=Decimal("100"), settled_at=1.0, divergence_logged=True,
        )
        store.save_settlement(settlement)
        loaded = store.settlement_for(market.slug)
        assert loaded is not None
        assert loaded.divergence_logged is True


class TestUnsettledMarketsAreForRestartReconciliation:
    def test_settled_and_dead_markets_are_excluded(self, store: Store) -> None:
        live = _market()
        settled = MarketInstance.create(window_ts=CLOSE_TS, offsets=OFFSETS)
        dead = MarketInstance.create(window_ts=CLOSE_TS + 300, offsets=OFFSETS)
        store.create_market(live, 1.0)
        store.create_market(settled, 1.0)
        store.create_market(dead, 1.0)
        store.save_phase(settled.slug, MarketPhase.SETTLED, 2.0)
        store.save_phase(dead.slug, MarketPhase.DEAD, 2.0)

        unsettled = store.unsettled_markets()
        assert live.slug in unsettled
        assert settled.slug not in unsettled
        assert dead.slug not in unsettled

    def test_active_and_cancelling_markets_are_included(self, store: Store) -> None:
        market = _market()
        store.create_market(market, 1.0)
        store.save_phase(market.slug, MarketPhase.CANCELLING, 2.0)
        assert market.slug in store.unsettled_markets()


class TestArchiveMarketNeverDeletesTheRow:
    """A8/A17: the recorded history is the dataset the waiting period produces."""

    def test_archive_sets_the_flag(self, store: Store) -> None:
        market = _market()
        store.create_market(market, 1.0)
        store.archive_market(market.slug, 2.0)
        row = store.load_market_row(market.slug)
        assert row is not None
        assert row["archived"] == 1

    def test_archive_does_not_remove_the_row(self, store: Store) -> None:
        market = _market()
        store.create_market(market, 1.0)
        store.archive_market(market.slug, 2.0)
        assert store.market_exists(market.slug) is True

    def test_archived_markets_still_appear_in_recent_markets(self, store: Store) -> None:
        market = _market()
        store.create_market(market, 1.0)
        store.archive_market(market.slug, 2.0)
        slugs = {r["slug"] for r in store.recent_markets()}
        assert market.slug in slugs


class TestSettingsPersistence:
    def test_no_settings_on_a_fresh_database(self, store: Store) -> None:
        assert store.has_settings() is False
        assert store.load_settings() == {}

    def test_save_and_load_round_trips(self, store: Store) -> None:
        store.save_settings({"a": "1", "b": "2"}, 1.0)
        assert store.has_settings() is True
        assert store.load_settings() == {"a": "1", "b": "2"}

    def test_save_settings_upserts_existing_keys(self, store: Store) -> None:
        store.save_settings({"a": "1"}, 1.0)
        store.save_settings({"a": "2", "c": "3"}, 2.0)
        assert store.load_settings() == {"a": "2", "c": "3"}

    def test_a_batch_that_fails_midway_rolls_back_as_a_whole(self, store: Store) -> None:
        """A partial apply would leave a new window list beside an old buffer set.

        save_settings() can't be driven to fail through its own signature (a
        dict cannot hold a duplicate key), so this exercises the same
        `with self._conn:` transaction pattern directly to prove a failure
        anywhere in the batch leaves NONE of it committed.
        """
        store.save_settings({"a": "1", "b": "1"}, 1.0)
        with pytest.raises(sqlite3.IntegrityError), store.connection:
            store.connection.executemany(
                "INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?)",
                [("z", "1", 2.0), ("z", "2", 2.0)],  # duplicate primary key
            )
        # the original two keys must still be exactly as they were, and "z" absent
        assert store.load_settings() == {"a": "1", "b": "1"}


class TestRuntimeStateRoundTrips:
    def test_missing_key_is_none(self, store: Store) -> None:
        assert store.get_runtime_state("nope") is None

    def test_set_then_get(self, store: Store) -> None:
        store.set_runtime_state("last_slug", "btc-updown-5m-1", 1.0)
        assert store.get_runtime_state("last_slug") == "btc-updown-5m-1"

    def test_set_overwrites(self, store: Store) -> None:
        store.set_runtime_state("k", "1", 1.0)
        store.set_runtime_state("k", "2", 2.0)
        assert store.get_runtime_state("k") == "2"


class TestCandlesAreResearchDataOnly:
    """A18: nothing in the trading path reads this table."""

    def test_save_and_count(self, store: Store) -> None:
        rows = [(WINDOW_TS, "1", "2", "0.5", "1.5", "10", "binance")]
        assert store.save_candles(rows) == 1
        assert store.candle_count() == 1

    def test_duplicate_open_ts_is_ignored(self, store: Store) -> None:
        row = (WINDOW_TS, "1", "2", "0.5", "1.5", "10", "binance")
        store.save_candles([row])
        store.save_candles([row])
        assert store.candle_count() == 1

    def test_candles_between_is_half_open(self, store: Store) -> None:
        store.save_candles([
            (WINDOW_TS, "1", "1", "1", "1", "1", "s"),
            (CLOSE_TS, "1", "1", "1", "1", "1", "s"),
        ])
        result = store.candles_between(WINDOW_TS, CLOSE_TS)
        assert len(result) == 1
        assert result[0]["open_ts"] == WINDOW_TS

    def test_save_candles_with_no_rows_is_a_no_op(self, store: Store) -> None:
        assert store.save_candles([]) == 0


class TestStoreIsAContextManager:
    def test_enter_returns_self(self, tmp_path: Path) -> None:
        with Store(tmp_path / "arc.db") as store:
            assert isinstance(store, Store)

    def test_exit_closes_the_connection(self, tmp_path: Path) -> None:
        with Store(tmp_path / "arc.db") as store:
            store.migrate(1.0)
        with pytest.raises(sqlite3.ProgrammingError):
            store.connection.execute("SELECT 1")


class TestOrderStateTransitionsPersist:
    def test_save_order_state_updates_state_and_reason(self, store: Store) -> None:
        market = _market()
        store.create_market(market, 1.0)
        order = Order(
            order_id="o1", market_slug=market.slug, offset_seconds=3,
            direction=Direction.UP, price=Decimal("0.5"), size=Decimal("10"),
        )
        store.save_order(order)
        store.save_order_state("o1", OrderState.REJECTED, 2.0, rejection_reason="too late")
        loaded = store.orders_for(market.slug)[0]
        assert loaded.state is OrderState.REJECTED
        assert loaded.rejection_reason == "too late"

    def test_save_order_upserts_on_conflict(self, store: Store) -> None:
        market = _market()
        store.create_market(market, 1.0)
        order = Order(
            order_id="o1", market_slug=market.slug, offset_seconds=3,
            direction=Direction.UP, price=Decimal("0.5"), size=Decimal("10"),
            state=OrderState.PENDING,
        )
        store.save_order(order)
        order.state = OrderState.SUBMITTED
        order.venue_order_id = "venue-123"
        store.save_order(order)
        loaded = store.orders_for(market.slug)
        assert len(loaded) == 1
        assert loaded[0].state is OrderState.SUBMITTED
        assert loaded[0].venue_order_id == "venue-123"
