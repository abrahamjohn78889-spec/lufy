"""The MAJORITY engine's pure core: configuration, identity, trigger, side lock.

Nothing here touches SQLite, a venue, or a clock. Every function under test is a
pure function of its arguments, which is what lets these tests assert on the exact
literal strings and decisions the engine will produce in production rather than on a
recomputation of the same formula.

The identity assertions are written as LITERALS on purpose. Asserting
`order_id_for(...) == order_id_for(...)` would pass even if both sides changed
together, and the whole point of those tests is that the TWAP form must never move.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from arc.domain.enums import DEFAULT_ENGINE, Direction
from arc.errors import ConfigInvariantError
from arc.execution.orders import chain_id_for, new_order, next_generation_id, order_id_for
from arc.majority.config import (
    MAJORITY_ENGINE,
    build_majority_config,
)
from arc.majority.identity import (
    ENGINE_TWAP,
    engine_prefix,
    majority_intent_id_for,
    majority_trace_id_for,
    majority_window_label,
)
from arc.majority.state import (
    MAJORITY_TERMINAL_STATES,
    MajorityMarketState,
    MajorityState,
    MajorityStateError,
)
from arc.majority.trigger import (
    BookSnapshot,
    MajorityOutcome,
    MajorityVerdict,
    determine_majority,
    is_triggered,
    trigger_value,
)

SLUG = "btc-updown-5m-1786263900"
WINDOW = 45
TICK = Decimal("0.01")
MIN_SIZE = Decimal("5")


def values(**overrides: str) -> dict[str, str]:
    """An enabled MAJORITY configuration that passes every invariant."""
    base = {
        "majority_enabled": "true",
        "majority_execution_windows": str(WINDOW),
        "majority_buffer": "0",
        "majority_trigger_price": "0.90",
        "majority_target_limit_price": "0.95",
        "majority_shares": "20",
        "majority_entry_price_min": "0.90",
        "majority_entry_price_max": "0.99",
    }
    base.update(overrides)
    return base


def build(**overrides: str):  # type: ignore[no-untyped-def]
    return build_majority_config(
        values(**overrides), min_tradable_size=MIN_SIZE, tick_size=TICK
    )


def book(up: str | None, down: str | None, *, fresh: bool = True) -> BookSnapshot:
    return BookSnapshot(
        best_bid_up=None if up is None else Decimal(up),
        best_bid_down=None if down is None else Decimal(down),
        read_at=1000.0,
        fresh=fresh,
    )


# ── configuration ────────────────────────────────────────────────────────────


class TestMajorityIsOffUntilItIsConfigured:
    """Absent keys mean NO ENGINE, not an engine running on invented numbers."""

    def test_absent_keys_disable_the_engine(self) -> None:
        config = build_majority_config({}, min_tradable_size=MIN_SIZE, tick_size=TICK)
        assert config.enabled is False
        assert config.tradable is False

    def test_a_disabled_engine_validates_nothing(self) -> None:
        """Every number blank and it still builds: nothing can reach the trading path."""
        config = build_majority_config(
            {"majority_enabled": "false", "majority_trigger_price": ""},
            min_tradable_size=MIN_SIZE,
            tick_size=TICK,
        )
        assert config.enabled is False

    def test_enabled_with_a_missing_number_is_fatal(self) -> None:
        """The moment it is on, every number must have been chosen by the operator."""
        with pytest.raises(ConfigInvariantError, match="TRIGGER_PRICE"):
            build(majority_trigger_price="")

    def test_an_enabled_configuration_is_tradable(self) -> None:
        assert build().tradable is True


class TestEveryWindowLengthHasAnApprovedBufferFormula:
    """The final spec defines the buffer for EVERY window length.

    >30s: BTC reference ± buffer become two internal memory triggers and the
    first one satisfied opens the execution opportunity. <=30s: the buffer
    enters the TWAP-supported entry calculation. Zero: direct entry. There is
    no length left without a formula, so the builder never fail-closes a
    window because of its length.
    """

    def test_a_long_window_with_a_buffer_is_tradable(self) -> None:
        config = build(majority_execution_windows="45", majority_buffer="1.00")
        assert config.enabled is True
        assert config.tradable is True
        assert config.windows_by_offset[0].disable_reason == ""

    def test_the_limit_itself_is_permitted_with_a_buffer(self) -> None:
        config = build(majority_execution_windows="30", majority_buffer="1.00")
        assert config.windows_by_offset[0].disable_reason == ""

    def test_one_second_past_the_limit_is_permitted_with_a_buffer(self) -> None:
        config = build(majority_execution_windows="31", majority_buffer="1.00")
        assert config.windows_by_offset[0].disable_reason == ""

    def test_a_zero_buffer_window_is_permitted_at_any_length(self) -> None:
        """No buffer mathematics at all: direct entry at the MAJORITY direction."""
        config = build(majority_execution_windows="45", majority_buffer="0")
        assert config.windows_by_offset[0].disable_reason == ""
        assert config.tradable is True


class TestConfigurationInvariantsThatWouldTradeWrongly:
    def test_a_window_at_or_past_market_close_is_fatal(self) -> None:
        with pytest.raises(ConfigInvariantError, match="not shorter than"):
            build(majority_execution_windows="300")

    def test_a_zero_window_is_fatal(self) -> None:
        with pytest.raises(ConfigInvariantError, match="non-positive"):
            build(majority_execution_windows="0")

    def test_a_negative_buffer_is_fatal(self) -> None:
        with pytest.raises(ConfigInvariantError, match="must not be negative"):
            build(majority_buffer="-1")

    @pytest.mark.parametrize("price", ["0", "-0.1", "-100"])
    def test_a_non_positive_trigger_is_fatal_at_any_scale(self, price: str) -> None:
        with pytest.raises(ConfigInvariantError, match="must be positive"):
            build(majority_trigger_price=price)

    @pytest.mark.parametrize("price", ["0", "-0.1", "-100"])
    def test_a_non_positive_target_is_fatal_at_any_scale(self, price: str) -> None:
        with pytest.raises(ConfigInvariantError, match="must be positive"):
            build(majority_target_limit_price=price)

    def test_btc_denominated_trigger_and_target_are_accepted(self) -> None:
        """A trigger above 1 is a BTC-PRICE level, not an error."""
        config = build(
            majority_trigger_price="65050",
            majority_target_limit_price="65051",
            majority_entry_price_min="65040",
            majority_entry_price_max="65060",
        )
        assert config.tradable is True

    def test_an_inverted_entry_band_is_fatal(self) -> None:
        with pytest.raises(ConfigInvariantError, match="inverted band"):
            build(majority_entry_price_min="0.99", majority_entry_price_max="0.90")

    def test_a_band_narrower_than_one_tick_is_fatal(self) -> None:
        with pytest.raises(ConfigInvariantError, match="narrower than"):
            build(
                majority_entry_price_min="0.900",
                majority_entry_price_max="0.905",
                majority_target_limit_price="0.90",
            )

    def test_a_target_above_the_band_is_fatal(self) -> None:
        """Gate 11 would deny every order while the deck looked correctly set."""
        with pytest.raises(ConfigInvariantError, match="above MAJORITY_ENTRY_PRICE_MAX"):
            build(majority_target_limit_price="0.995", majority_entry_price_max="0.99")

    def test_a_target_below_the_band_is_fatal(self) -> None:
        with pytest.raises(ConfigInvariantError, match="below MAJORITY_ENTRY_PRICE_MIN"):
            build(majority_target_limit_price="0.80", majority_entry_price_min="0.90")

    def test_shares_below_the_exchange_minimum_are_refused_not_rounded(self) -> None:
        """Rounding up would trade a size the operator never chose."""
        with pytest.raises(ConfigInvariantError, match="refused rather than rounded"):
            build(majority_shares="1")

    def test_zero_shares_is_fatal(self) -> None:
        with pytest.raises(ConfigInvariantError, match="must be positive"):
            build(majority_shares="0")

    def test_a_trigger_above_the_target_warns_but_is_permitted(self) -> None:
        config = build(majority_trigger_price="0.98", majority_target_limit_price="0.95")
        assert config.tradable is True
        assert any("above" in w for w in config.warnings)


class TestConfigurationIsFrozenAndSerialisable:
    def test_the_config_cannot_be_mutated_after_validation(self) -> None:
        config = build()
        with pytest.raises((AttributeError, TypeError)):
            config.shares = Decimal("999")

    def test_storage_round_trip_keeps_every_value(self) -> None:
        stored = build().as_storage_dict()
        rebuilt = build_majority_config(stored, min_tradable_size=MIN_SIZE, tick_size=TICK)
        assert rebuilt.windows_by_offset[0].shares == build().windows_by_offset[0].shares
        assert rebuilt.windows_by_offset[0].trigger_price == build().windows_by_offset[0].trigger_price
        assert (
            rebuilt.windows_by_offset[0].execution_window_seconds
            == build().windows_by_offset[0].execution_window_seconds
        )

    def test_every_stored_value_is_text(self) -> None:
        assert all(isinstance(v, str) for v in build().as_storage_dict().values())


# ── identity ─────────────────────────────────────────────────────────────────


class TestTwapIdentityIsByteIdenticalForever:
    """The frozen contract. These are literals, never recomputations."""

    def test_chain_id_is_unchanged(self) -> None:
        assert chain_id_for(SLUG, 3, 0) == "btc-updown-5m-1786263900:3:0"

    def test_order_id_is_unchanged(self) -> None:
        assert order_id_for(SLUG, 3, 0, 0) == "btc-updown-5m-1786263900:3:0:0"

    def test_a_later_generation_is_unchanged(self) -> None:
        assert order_id_for(SLUG, 3, 0, 2) == "btc-updown-5m-1786263900:3:0:2"

    def test_passing_the_default_engine_explicitly_changes_nothing(self) -> None:
        assert order_id_for(SLUG, 3, 0, 0, DEFAULT_ENGINE) == order_id_for(SLUG, 3, 0, 0)

    def test_the_default_engine_prefix_is_empty(self) -> None:
        """If this ever became "TWAP:" every resting order would orphan on restart."""
        assert engine_prefix(ENGINE_TWAP) == ""

    def test_the_default_engine_name_matches_the_identity_module(self) -> None:
        assert DEFAULT_ENGINE == ENGINE_TWAP == "TWAP"


class TestMajorityIdentityCannotCollideWithTwap:
    def test_the_engine_prefix_is_qualified(self) -> None:
        assert engine_prefix(MAJORITY_ENGINE) == "MAJORITY:"

    def test_order_ids_differ_for_identical_inputs(self) -> None:
        """Case A: same market, window, index and generation, two engines."""
        twap = order_id_for(SLUG, WINDOW, 0, 0)
        majority = order_id_for(SLUG, WINDOW, 0, 0, MAJORITY_ENGINE)
        assert twap == "btc-updown-5m-1786263900:45:0:0"
        assert majority == "MAJORITY:btc-updown-5m-1786263900:45:0:0"
        assert twap != majority

    def test_chain_ids_differ_for_identical_inputs(self) -> None:
        assert chain_id_for(SLUG, WINDOW, 0) != chain_id_for(SLUG, WINDOW, 0, MAJORITY_ENGINE)

    def test_intent_ids_differ(self) -> None:
        assert majority_intent_id_for(SLUG, WINDOW) == f"MAJORITY:{SLUG}:45"
        assert majority_intent_id_for(SLUG, WINDOW) != f"{SLUG}:45"

    def test_trace_ids_differ_from_the_twap_derivation(self) -> None:
        """Same length and shape, provably different value."""
        import hashlib

        twap_trace = hashlib.sha256(f"{SLUG}:{WINDOW}".encode()).hexdigest()[:24]
        assert majority_trace_id_for(SLUG, WINDOW) != twap_trace
        assert len(majority_trace_id_for(SLUG, WINDOW)) == len(twap_trace)

    def test_identity_is_pure_across_calls(self) -> None:
        """A replay after a crash must recompute the same id, not a new one."""
        assert majority_intent_id_for(SLUG, WINDOW) == majority_intent_id_for(SLUG, WINDOW)
        assert majority_trace_id_for(SLUG, WINDOW) == majority_trace_id_for(SLUG, WINDOW)


class TestRepriceChainsSurviveThePrefix:
    """The generation must stay rightmost, or reprice breaks for one engine."""

    def _order(self, engine: str, generation: int):  # type: ignore[no-untyped-def]
        return new_order(
            market_slug=SLUG,
            offset_seconds=WINDOW,
            index=0,
            generation=generation,
            direction=Direction.UP,
            price=Decimal("0.95"),
            size=Decimal("20"),
            now=1000.0,
            engine=engine,
        )

    @pytest.mark.parametrize("engine", [DEFAULT_ENGINE, MAJORITY_ENGINE])
    def test_generation_advances_zero_to_one_to_two(self, engine: str) -> None:
        """Cases B and C: both engines' reprice chains advance independently."""
        gen0 = self._order(engine, 0)
        assert next_generation_id(gen0).endswith(":1")
        gen1 = self._order(engine, 1)
        assert next_generation_id(gen1).endswith(":2")

    def test_the_majority_successor_keeps_the_engine_prefix(self) -> None:
        """A successor that lost the prefix would escape every engine-scoped sweep."""
        gen0 = self._order(MAJORITY_ENGINE, 0)
        assert next_generation_id(gen0) == "MAJORITY:btc-updown-5m-1786263900:45:0:1"

    def test_new_order_stamps_the_engine_on_the_row(self) -> None:
        assert self._order(MAJORITY_ENGINE, 0).engine == MAJORITY_ENGINE
        assert self._order(DEFAULT_ENGINE, 0).engine == DEFAULT_ENGINE

    def test_new_order_defaults_to_the_frozen_engine(self) -> None:
        order = new_order(
            market_slug=SLUG,
            offset_seconds=3,
            index=0,
            generation=0,
            direction=Direction.UP,
            price=Decimal("0.80"),
            size=Decimal("31"),
            now=1000.0,
        )
        assert order.engine == DEFAULT_ENGINE
        assert order.order_id == "btc-updown-5m-1786263900:3:0:0"


# ── trigger ──────────────────────────────────────────────────────────────────


class TestTheTriggerIsMaxOfBothBids:
    def test_the_maximum_is_taken_not_a_chosen_side(self) -> None:
        assert trigger_value(book("0.16", "0.85")) == Decimal("0.85")
        assert trigger_value(book("0.85", "0.16")) == Decimal("0.85")

    def test_a_missing_side_yields_no_trigger_value(self) -> None:
        """Never the one present bid: an uncomparable book must not fire."""
        assert trigger_value(book(None, "0.95")) is None
        assert trigger_value(book("0.95", None)) is None

    def test_the_threshold_is_inclusive(self) -> None:
        """0.90 exactly is the ordinary case on a tick-sized book, not an edge."""
        assert is_triggered(book("0.90", "0.10"), Decimal("0.90")) is True

    def test_below_the_threshold_does_not_fire(self) -> None:
        assert is_triggered(book("0.89", "0.11"), Decimal("0.90")) is False

    def test_a_stale_book_never_fires(self) -> None:
        assert is_triggered(book("0.99", "0.01", fresh=False), Decimal("0.90")) is False

    def test_an_incomplete_book_never_fires(self) -> None:
        assert is_triggered(book("0.99", None), Decimal("0.90")) is False


class TestTheMajorityComparisonIsStrict:
    def test_the_higher_bid_wins(self) -> None:
        assert determine_majority(book("0.85", "0.16")).outcome is MajorityOutcome.UP
        assert determine_majority(book("0.16", "0.85")).outcome is MajorityOutcome.DOWN

    def test_the_screenshot_case_selects_down(self) -> None:
        """UP 0.16 / DOWN 0.85: the verdict is DOWN, and the side is a real one."""
        verdict = determine_majority(book("0.16", "0.85"))
        assert verdict.outcome is MajorityOutcome.DOWN
        assert verdict.direction is Direction.DOWN
        assert verdict.tradable is True

    def test_equal_bids_are_indeterminate_never_a_tie_break(self) -> None:
        verdict = determine_majority(book("0.50", "0.50"))
        assert verdict.outcome is MajorityOutcome.INDETERMINATE
        assert verdict.direction is None
        assert verdict.tradable is False

    def test_a_stale_book_is_indeterminate(self) -> None:
        verdict = determine_majority(book("0.85", "0.16", fresh=False))
        assert verdict.outcome is MajorityOutcome.INDETERMINATE
        assert "stale" in verdict.reason

    @pytest.mark.parametrize(
        ("up", "down"), [(None, "0.85"), ("0.85", None), (None, None)]
    )
    def test_a_missing_side_is_indeterminate(self, up: str | None, down: str | None) -> None:
        assert determine_majority(book(up, down)).outcome is MajorityOutcome.INDETERMINATE

    def test_the_verdict_carries_the_numbers_it_was_made_from(self) -> None:
        """Recomputing later would read a book that has since moved."""
        verdict = determine_majority(book("0.16", "0.85"))
        assert verdict.best_bid_up == Decimal("0.16")
        assert verdict.best_bid_down == Decimal("0.85")

    def test_a_one_tick_difference_still_decides(self) -> None:
        assert determine_majority(book("0.51", "0.50")).outcome is MajorityOutcome.UP


# ── the side lock ────────────────────────────────────────────────────────────


class TestTheSideLockIsWriteOnce:
    def _state(self) -> MajorityMarketState:
        return MajorityMarketState(
            market_slug=SLUG, close_ts=1786264200, execution_window_seconds=WINDOW
        )

    def test_a_fresh_state_has_no_side(self) -> None:
        state = self._state()
        assert state.selected_side is None
        assert state.side_locked is False

    def test_selecting_locks_the_side(self) -> None:
        state = self._state()
        snapshot = book("0.16", "0.85")
        state.select_side(determine_majority(snapshot), snapshot, 1000.0)
        assert state.selected_side is Direction.DOWN
        assert state.side_locked is True
        assert state.state is MajorityState.SIDE_SELECTED

    def test_a_second_selection_is_refused_even_with_the_same_side(self) -> None:
        """A repeat call means some path believes it may re-derive the side."""
        state = self._state()
        snapshot = book("0.16", "0.85")
        state.select_side(determine_majority(snapshot), snapshot, 1000.0)
        with pytest.raises(MajorityStateError, match="immutable"):
            state.select_side(determine_majority(snapshot), snapshot, 1001.0)

    def test_a_second_selection_with_the_opposite_side_is_refused(self) -> None:
        state = self._state()
        first = book("0.16", "0.85")
        state.select_side(determine_majority(first), first, 1000.0)
        flipped = book("0.85", "0.16")
        with pytest.raises(MajorityStateError):
            state.select_side(determine_majority(flipped), flipped, 1001.0)
        assert state.selected_side is Direction.DOWN

    def test_an_indeterminate_verdict_cannot_lock_a_side(self) -> None:
        state = self._state()
        tied = book("0.50", "0.50")
        with pytest.raises(MajorityStateError, match="INDETERMINATE"):
            state.select_side(determine_majority(tied), tied, 1000.0)
        assert state.selected_side is None

    def test_there_is_no_setter_for_the_locked_side(self) -> None:
        state = self._state()
        with pytest.raises(AttributeError):
            state.selected_side = Direction.UP  # type: ignore[misc]


class TestTheTriggerFiresOncePerMarket:
    def _state(self) -> MajorityMarketState:
        return MajorityMarketState(
            market_slug=SLUG, close_ts=1786264200, execution_window_seconds=WINDOW
        )

    def test_a_fresh_state_has_not_triggered(self) -> None:
        assert self._state().triggered is False

    def test_marking_triggered_records_the_snapshot_and_instant(self) -> None:
        state = self._state()
        snapshot = book("0.95", "0.05")
        state.mark_triggered(snapshot, 1000.0)
        assert state.triggered is True
        assert state.triggered_at == 1000.0
        assert state.trigger_snapshot is snapshot
        assert state.state is MajorityState.TRIGGERED

    def test_a_second_firing_is_refused(self) -> None:
        state = self._state()
        state.mark_triggered(book("0.95", "0.05"), 1000.0)
        with pytest.raises(MajorityStateError, match="fires once"):
            state.mark_triggered(book("0.96", "0.04"), 1001.0)

    def test_the_decision_snapshot_is_separate_from_the_trigger_snapshot(self) -> None:
        """The two-step rule is auditable, not merely intended."""
        state = self._state()
        trigger_book = book("0.95", "0.05")
        state.mark_triggered(trigger_book, 1000.0)
        decision_book = book("0.16", "0.85")
        state.select_side(determine_majority(decision_book), decision_book, 1001.0)
        assert state.trigger_snapshot is trigger_book
        assert state.decision_snapshot is decision_book
        assert state.trigger_snapshot is not state.decision_snapshot


class TestTerminalStatesAreFinal:
    def _state(self) -> MajorityMarketState:
        return MajorityMarketState(
            market_slug=SLUG, close_ts=1786264200, execution_window_seconds=WINDOW
        )

    def test_no_trade_is_terminal(self) -> None:
        state = self._state()
        state.mark_no_trade("indeterminate")
        assert state.terminal is True
        assert state.state is MajorityState.NO_TRADE
        assert state.no_trade_reason == "indeterminate"

    def test_no_trade_never_overwrites_a_filled_order(self) -> None:
        """Writing NO_TRADE over FILLED would erase a real position."""
        state = self._state()
        state.mark_state(MajorityState.FILLED)
        state.mark_no_trade("late")
        assert state.state is MajorityState.FILLED

    def test_a_terminal_state_does_not_advance(self) -> None:
        state = self._state()
        state.mark_no_trade("done")
        state.mark_state(MajorityState.WORKING)
        assert state.state is MajorityState.NO_TRADE

    def test_every_terminal_member_reports_terminal(self) -> None:
        for member in MAJORITY_TERMINAL_STATES:
            state = self._state()
            state.state = member
            assert state.terminal is True

    def test_the_engine_has_no_reset_path(self) -> None:
        """State is discarded per market (A11), never reused."""
        state = self._state()
        for forbidden in ("reset", "clear", "reuse", "reinit", "recycle"):
            assert not hasattr(state, forbidden)


# ── multi-window ──────────────────────────────────────────────────────────────


class TestMultiWindowConfiguration:
    """MAJORITY's config holds a tuple of windows, each validated independently."""

    def test_three_windows_are_all_built(self) -> None:
        config = build(majority_execution_windows="3,15,45")
        offsets = [w.execution_window_seconds for w in config.windows_by_offset]
        assert offsets == [3, 15, 45]

    def test_duplicate_windows_are_deduplicated(self) -> None:
        config = build(majority_execution_windows="15,15,3,3")
        offsets = [w.execution_window_seconds for w in config.windows_by_offset]
        assert offsets == [3, 15]

    def test_unsorted_windows_are_sorted_ascending(self) -> None:
        config = build(majority_execution_windows="45,3,15")
        offsets = [w.execution_window_seconds for w in config.windows_by_offset]
        assert offsets == [3, 15, 45]

    def test_a_long_window_never_disables_a_short_one(self) -> None:
        """The final spec gives every window length a buffer formula, so a
        15s/buffer-1 and a 45s/buffer-1 pair are BOTH tradable."""
        config = build(majority_execution_windows="15,45", majority_buffer="1.00")
        windows_by_offset = {w.execution_window_seconds: w for w in config.windows_by_offset}
        assert windows_by_offset[15].disable_reason == ""
        assert windows_by_offset[45].disable_reason == ""
        assert config.tradable is True
        assert [w.execution_window_seconds for w in config.tradable_windows] == [15, 45]

    def test_no_windows_means_disabled(self) -> None:
        config = build(majority_enabled="true", majority_execution_windows="")
        assert config.enabled is True
        assert config.windows_by_offset == ()
        # No windows means no engine output — tradable is False even though the
        # operator's intent was to enable it. The engine reports OFF, the
        # dashboard reads the empty windows list.
        assert config.tradable is False
        assert config.tradable_windows == ()

    def test_invalid_window_value_fatal(self) -> None:
        with pytest.raises(ConfigInvariantError, match="non-integer"):
            build(majority_execution_windows="15,abc,3")

    def test_window_for_lookup(self) -> None:
        config = build(majority_execution_windows="3,15,45")
        assert config.window_for(15) is not None
        assert config.window_for(15).execution_window_seconds == 15
        assert config.window_for(999) is None

    def test_storage_round_trip_preserves_multi_window(self) -> None:
        original = build(majority_execution_windows="3,15,45")
        stored = original.as_storage_dict()
        rebuilt = build_majority_config(stored, min_tradable_size=MIN_SIZE, tick_size=TICK)
        assert (
            rebuilt.windows_by_offset[0].execution_window_seconds
            == original.windows_by_offset[0].execution_window_seconds
        )
        assert (
            rebuilt.windows_by_offset[2].execution_window_seconds
            == original.windows_by_offset[2].execution_window_seconds
        )

    def test_legacy_scalar_is_still_accepted(self) -> None:
        """A pre-multi-window .env written `majority_execution_window_seconds=45`
        must boot exactly as it did before. The loader converts the scalar to a
        one-element window list, so the engine sees the same single-window shape
        the previous code did.
        """
        legacy = build_majority_config(
            {"majority_enabled": "true", "majority_execution_window_seconds": "45",
             "majority_buffer": "0", "majority_trigger_price": "0.90",
             "majority_target_limit_price": "0.95", "majority_shares": "20",
             "majority_entry_price_min": "0.90", "majority_entry_price_max": "0.99"},
            min_tradable_size=MIN_SIZE, tick_size=TICK,
        )
        assert [w.execution_window_seconds for w in legacy.windows_by_offset] == [45]

    def test_window_label_is_seconds_suffixed(self) -> None:
        assert majority_window_label(45) == "45s"
        assert majority_window_label(3) == "3s"


class TestPersistentSideLockReconstruction:
    """The side lock survives a restart by being reconstructed from the persisted
    intent. Without this, a restart would either re-determine the side (re-deriving
    it from a book that has since moved) or refuse to lock it again (orphaning the
    resting order). reconstruct_locked_side does the one safe thing."""

    def test_a_reconstructed_state_has_a_locked_side(self) -> None:
        state = MajorityMarketState(
            market_slug=SLUG, close_ts=1786264200, execution_window_seconds=WINDOW
        )
        state.reconstruct_locked_side(Direction.UP, intent_created_at=1234.0)
        assert state.selected_side is Direction.UP
        assert state.side_locked is True
        assert state.state is MajorityState.SIDE_SELECTED

    def test_a_subsequent_select_side_after_reconstruction_is_refused(self) -> None:
        """The reconstruction is the ONE lock for this window. select_side must
        refuse a second call afterwards — the same write-once rule that holds in
        a fresh run, persisted across the restart boundary.
        """
        state = MajorityMarketState(
            market_slug=SLUG, close_ts=1786264200, execution_window_seconds=WINDOW
        )
        state.reconstruct_locked_side(Direction.UP, intent_created_at=1234.0)
        with pytest.raises(MajorityStateError, match="immutable"):
            state.select_side(
                _up_verdict(),
                book("0.85", "0.16"),
                now=1235.0,
            )


def _up_verdict() -> MajorityVerdict:
    """A UP verdict whose majority decision is UP. Used to assert the lock
    refuses a same-side retry, which is the case the test cares about.
    """
    return MajorityVerdict(
        outcome=MajorityOutcome.UP,
        best_bid_up=Decimal("0.85"),
        best_bid_down=Decimal("0.16"),
        reason="UP bid is higher",
    )
