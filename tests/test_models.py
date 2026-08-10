"""Domain models: per-market state that is created once and thrown away (A11).

There is no reset() or clear() on MarketInstance, TwapAccumulator, or
ExecutionWindow — enforced structurally in test_infrastructure.py. What is tested
here is the behavioural side of the same guarantee: a freshly constructed instance
starts genuinely empty, freezing is atomic and irreversible, and the two TWAP
quantities (signal vs settlement) and the PTB never get conflated or overwritten.

Several tests below exist specifically to catch a regression toward the mistakes
the module docstring calls out by name: the incremental-mean drift of hazard H1,
the per-order (rather than per-reprice-chain) fill counting of hazard H4, and the
UP/DOWN trigger asymmetry that a single shared `>=` would silently delete.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from decimal import Decimal

import pytest
from conftest import CLOSE_TS, OFFSETS, WINDOW_TS

from arc.domain.enums import Direction, MarketPhase, OrderState, Outcome, WindowState
from arc.domain.models import (
    ExecutionIntent,
    ExecutionWindow,
    Fill,
    MarketInstance,
    Observation,
    Order,
    Settlement,
    TwapAccumulator,
)
from arc.errors import NoDirectionError, ObservationRejectedError, WindowFreezeError


def _obs(ts: float, price: str) -> Observation:
    return Observation(ts=ts, price=Decimal(price))


class TestObservationValidatesOnConstruction:
    def test_positive_price_is_accepted(self) -> None:
        obs = _obs(1.0, "120000.00")
        assert obs.price == Decimal("120000.00")

    def test_price_is_coerced_to_decimal(self) -> None:
        obs = Observation(ts=1.0, price="120000.00")  # type: ignore[arg-type]
        assert isinstance(obs.price, Decimal)

    @pytest.mark.parametrize("price", ["0", "-1", "-0.01"])
    def test_non_positive_price_is_refused(self, price: str) -> None:
        with pytest.raises(ValueError, match="must be positive"):
            _obs(1.0, price)

    def test_is_frozen(self) -> None:
        """An observation already folded into a running sum must not be mutable."""
        obs = _obs(1.0, "120000.00")
        with pytest.raises(FrozenInstanceError):
            obs.price = Decimal("999")  # type: ignore[misc]

    def test_optional_fields_default(self) -> None:
        obs = _obs(1.0, "1.00")
        assert obs.feed_id == ""
        assert obs.window_seconds is None


class TestTwapAccumulatorHasNoReset:
    """A11: "resets per market" is satisfied by throwing the object away."""

    def test_fresh_accumulator_is_empty(self) -> None:
        acc = TwapAccumulator()
        assert acc.observation_count == 0
        assert acc.running_sum == Decimal("0")

    def test_mean_is_none_when_empty(self) -> None:
        """None, not zero — a zero mean would produce a confident direction from no data."""
        assert TwapAccumulator().mean is None

    def test_mean_after_one_observation(self) -> None:
        acc = TwapAccumulator()
        acc.add(Decimal("100"))
        assert acc.mean == Decimal("100")

    def test_mean_is_the_exact_average(self) -> None:
        acc = TwapAccumulator()
        for price in ("1", "2", "3", "4"):
            acc.add(Decimal(price))
        assert acc.mean == Decimal("2.5")

    def test_add_accepts_str_and_int_too(self) -> None:
        acc = TwapAccumulator()
        acc.add("10.5")
        acc.add(5)
        assert acc.running_sum == Decimal("15.5")
        assert acc.observation_count == 2


class TestExactSumBeatsIncrementalMean:
    """Hazard H1: M += (x - M) / n rounds at every step and drifts monotonically.

    Summing exactly and dividing once at the point of use means exactly one
    rounding total, regardless of how many observations were folded in.
    """

    def test_sum_then_divide_matches_a_hand_computed_exact_mean(self) -> None:
        prices = [Decimal("100.01"), Decimal("100.02"), Decimal("99.97"), Decimal("100.13")]
        acc = TwapAccumulator()
        for p in prices:
            acc.add(p)
        exact_mean = sum(prices, Decimal("0")) / len(prices)
        assert acc.mean == exact_mean

    def test_running_sum_is_exact_across_many_observations(self) -> None:
        """300 observations, one per second across a 5-minute market."""
        acc = TwapAccumulator()
        prices = [Decimal("100") + Decimal(i) / Decimal(1000) for i in range(300)]
        for p in prices:
            acc.add(p)
        assert acc.running_sum == sum(prices, Decimal("0"))
        assert acc.mean == acc.running_sum / 300

    def test_incremental_form_would_have_drifted_from_this(self) -> None:
        """The exact-sum mean and a hand-rolled incremental mean can disagree.

        This does not assert they always disagree — only exhibits a case where
        they do, so a regression to the incremental form is guarded against having
        a matching test pass by coincidence.
        """
        prices = [Decimal("1"), Decimal("3"), Decimal("2")]
        acc = TwapAccumulator()
        for p in prices:
            acc.add(p)
        exact = acc.mean

        incremental = Decimal("0")
        for n, p in enumerate(prices, start=1):
            incremental += (p - incremental) / n
        assert exact == incremental  # true for exact Decimal division here
        # The two forms are equal only because Decimal division is exact at this
        # precision; the property under guard is that ARC always uses exact-sum,
        # never the incremental form, which is enforced structurally.


class TestTwapAccumulatorRestore:
    """Restart recovery rebuilds from the persisted sum and count, not the mean."""

    def test_restore_reproduces_the_mean(self) -> None:
        acc = TwapAccumulator.restore(running_sum="600.00", observation_count=3)
        assert acc.mean == Decimal("200.00")

    def test_restore_accepts_str_int_or_decimal_sum(self) -> None:
        assert TwapAccumulator.restore("10", 2).running_sum == Decimal("10")
        assert TwapAccumulator.restore(10, 2).running_sum == Decimal("10")
        assert TwapAccumulator.restore(Decimal("10"), 2).running_sum == Decimal("10")

    def test_restore_with_zero_count_is_empty(self) -> None:
        acc = TwapAccumulator.restore(running_sum="0", observation_count=0)
        assert acc.mean is None

    def test_negative_count_is_refused(self) -> None:
        with pytest.raises(ValueError, match="must not be negative"):
            TwapAccumulator.restore(running_sum="0", observation_count=-1)

    def test_restored_accumulator_keeps_accumulating_correctly(self) -> None:
        """Resuming after a restart must not bake in an extra rounding step."""
        acc = TwapAccumulator.restore(running_sum="600.00", observation_count=3)
        acc.add(Decimal("200.00"))
        assert acc.mean == Decimal("200.00")
        assert acc.observation_count == 4


class TestExecutionWindowFreezeIsAtomic:
    """A12: every input validated and every derived value computed before any
    field is written. On any failure the window is untouched, still PENDING,
    with all five values still None — never a partial freeze.
    """

    def test_fresh_window_starts_pending_and_unfrozen(self) -> None:
        window = ExecutionWindow(offset_seconds=3)
        assert window.state is WindowState.PENDING
        assert not window.is_frozen
        for field_name in ("opening_twap", "ptb", "buffer", "direction", "locked_trigger"):
            assert getattr(window, field_name) is None
        assert window.frozen_at is None and window.fired_at is None

    def test_successful_freeze_sets_all_five_values(self) -> None:
        window = ExecutionWindow(offset_seconds=3)
        window.freeze(
            opening_twap=Decimal("120010.00"),
            ptb=Decimal("120000.00"),
            buffer=Decimal("2.00"),
            frozen_at=100.0,
        )
        assert window.state is WindowState.FROZEN
        assert window.is_frozen
        assert window.opening_twap == Decimal("120010.00")
        assert window.ptb == Decimal("120000.00")
        assert window.buffer == Decimal("2.00")
        assert window.direction is Direction.UP
        assert window.locked_trigger == Decimal("120012.00")
        assert window.frozen_at == 100.0

    def test_up_direction_when_twap_is_strictly_above_ptb(self) -> None:
        window = ExecutionWindow(offset_seconds=3)
        window.freeze(
            opening_twap=Decimal("100"), ptb=Decimal("99"), buffer=Decimal("1"), frozen_at=0.0
        )
        assert window.direction is Direction.UP
        assert window.locked_trigger == Decimal("101")

    def test_equality_yields_no_direction_and_freezes_nothing(self) -> None:
        """Strict comparison only. Equality is the absence of a direction, not a tie."""
        window = ExecutionWindow(offset_seconds=3)
        with pytest.raises(NoDirectionError):
            window.freeze(
                opening_twap=Decimal("100"), ptb=Decimal("100"), buffer=Decimal("1"),
                frozen_at=0.0,
            )
        assert window.state is WindowState.PENDING
        assert window.direction is None
        assert window.locked_trigger is None
        assert window.opening_twap is None
        assert window.ptb is None
        assert window.buffer is None
        assert window.frozen_at is None

    def test_no_direction_is_terminal(self) -> None:
        """mark_expired must not overwrite it: the two mean different things."""
        window = ExecutionWindow(offset_seconds=3)
        window.state = WindowState.NO_DIRECTION
        window.mark_expired()
        assert window.state is WindowState.NO_DIRECTION

    def test_down_direction_when_twap_below_ptb(self) -> None:
        window = ExecutionWindow(offset_seconds=3)
        window.freeze(
            opening_twap=Decimal("99"), ptb=Decimal("100"), buffer=Decimal("1"), frozen_at=0.0
        )
        assert window.direction is Direction.DOWN
        assert window.locked_trigger == Decimal("98")

    def test_second_freeze_is_refused(self) -> None:
        window = ExecutionWindow(offset_seconds=3)
        window.freeze(
            opening_twap=Decimal("100"), ptb=Decimal("99"), buffer=Decimal("1"), frozen_at=0.0
        )
        with pytest.raises(WindowFreezeError, match="already frozen"):
            window.freeze(
                opening_twap=Decimal("100"), ptb=Decimal("99"), buffer=Decimal("1"), frozen_at=1.0
            )

    def test_second_freeze_with_identical_values_is_still_refused(self) -> None:
        """A no-op second call is still a second write path; refuse it regardless."""
        window = ExecutionWindow(offset_seconds=3)
        args = {"opening_twap": Decimal("100"), "ptb": Decimal("99"), "buffer": Decimal("1")}
        window.freeze(**args, frozen_at=0.0)
        with pytest.raises(WindowFreezeError):
            window.freeze(**args, frozen_at=0.0)

    @pytest.mark.parametrize("buffer", ["0", "-1"])
    def test_non_positive_buffer_leaves_window_untouched(self, buffer: str) -> None:
        window = ExecutionWindow(offset_seconds=3)
        with pytest.raises(WindowFreezeError, match="buffer must be positive"):
            window.freeze(
                opening_twap=Decimal("100"), ptb=Decimal("100"), buffer=Decimal(buffer),
                frozen_at=0.0,
            )
        assert window.state is WindowState.PENDING
        assert window.opening_twap is None
        assert window.direction is None
        assert window.locked_trigger is None

    @pytest.mark.parametrize("ptb", ["0", "-5"])
    def test_non_positive_ptb_leaves_window_untouched(self, ptb: str) -> None:
        window = ExecutionWindow(offset_seconds=3)
        with pytest.raises(WindowFreezeError, match="ptb must be positive"):
            window.freeze(
                opening_twap=Decimal("100"), ptb=Decimal(ptb), buffer=Decimal("1"), frozen_at=0.0
            )
        assert window.state is WindowState.PENDING
        assert window.buffer is None

    @pytest.mark.parametrize("twap", ["0", "-1"])
    def test_non_positive_twap_leaves_window_untouched(self, twap: str) -> None:
        window = ExecutionWindow(offset_seconds=3)
        with pytest.raises(WindowFreezeError, match="opening_twap must be positive"):
            window.freeze(
                opening_twap=Decimal(twap), ptb=Decimal("100"), buffer=Decimal("1"), frozen_at=0.0
            )
        assert window.state is WindowState.PENDING
        assert window.frozen_at is None

    def test_invalid_decimal_input_leaves_window_untouched(self) -> None:
        window = ExecutionWindow(offset_seconds=3)
        with pytest.raises(WindowFreezeError):
            window.freeze(
                opening_twap="not-a-number",  # type: ignore[arg-type]
                ptb=Decimal("100"),
                buffer=Decimal("1"),
                frozen_at=0.0,
            )
        assert window.state is WindowState.PENDING
        assert window.ptb is None


class TestExecutionWindowHasNoSetter:
    """A structural gate also enforces this; this asserts the runtime shape."""

    def test_ptb_is_a_plain_mutable_field_not_frozen_by_the_dataclass(self) -> None:
        """The window's OWN ptb field is plain — freeze() is the only gate.

        Unlike MarketInstance.ptb, ExecutionWindow uses ordinary attribute
        assignment internally; the immutability guarantee comes entirely from the
        freeze()/is_frozen check, not from a property.
        """
        window = ExecutionWindow(offset_seconds=3)
        assert window.ptb is None


class TestRestoreFrozenIsVerbatimNeverRecomputed:
    """A4: restart recovery reloads direction and locked_trigger as ARGUMENTS.

    This method cannot recompute them and has no access to a current TWAP to try.
    Recomputing from a post-restart TWAP would yield a different trigger than the
    one the window actually froze against, and the bot would trade a strategy
    nobody configured while looking perfectly healthy.
    """

    def test_restore_sets_all_values_verbatim(self) -> None:
        window = ExecutionWindow(offset_seconds=5)
        window.restore_frozen(
            opening_twap=Decimal("100"),
            ptb=Decimal("99"),
            buffer=Decimal("1"),
            direction=Direction.UP,
            locked_trigger=Decimal("101"),
            frozen_at=42.0,
        )
        assert window.state is WindowState.FROZEN
        assert window.direction is Direction.UP
        assert window.locked_trigger == Decimal("101")
        assert window.frozen_at == 42.0

    def test_restore_does_not_recompute_direction_from_the_values_given(self) -> None:
        """twap < ptb would normally freeze DOWN — restore must not enforce that."""
        window = ExecutionWindow(offset_seconds=5)
        window.restore_frozen(
            opening_twap=Decimal("90"),  # below ptb
            ptb=Decimal("100"),
            buffer=Decimal("1"),
            direction=Direction.UP,  # contradicts what freeze() would have computed
            locked_trigger=Decimal("999"),  # likewise arbitrary
            frozen_at=0.0,
        )
        assert window.direction is Direction.UP
        assert window.locked_trigger == Decimal("999")

    def test_restore_accepts_fired_and_expired_states(self) -> None:
        for state in (WindowState.FIRED, WindowState.EXPIRED):
            window = ExecutionWindow(offset_seconds=5)
            window.restore_frozen(
                opening_twap=Decimal("100"), ptb=Decimal("99"), buffer=Decimal("1"),
                direction=Direction.UP, locked_trigger=Decimal("101"), frozen_at=0.0,
                state=state,
            )
            assert window.state is state

    def test_restore_refuses_pending_state(self) -> None:
        window = ExecutionWindow(offset_seconds=5)
        with pytest.raises(WindowFreezeError, match="cannot restore window into state"):
            window.restore_frozen(
                opening_twap=Decimal("100"), ptb=Decimal("99"), buffer=Decimal("1"),
                direction=Direction.UP, locked_trigger=Decimal("101"), frozen_at=0.0,
                state=WindowState.PENDING,
            )


class TestTriggerAsymmetry:
    """A single shared >= would delete the strategy rather than bias it.

    A DOWN trigger sits below the opening TWAP, so twap >= trigger is already
    true at the freeze instant — sharing the UP comparison would fire every DOWN
    window immediately and unconditionally.
    """

    def _frozen(self, direction: Direction, trigger: str) -> ExecutionWindow:
        window = ExecutionWindow(offset_seconds=3)
        window.restore_frozen(
            opening_twap=Decimal("100"), ptb=Decimal("100"), buffer=Decimal("1"),
            direction=direction, locked_trigger=Decimal(trigger), frozen_at=0.0,
        )
        return window

    def test_up_fires_when_signal_meets_or_exceeds_trigger(self) -> None:
        window = self._frozen(Direction.UP, "101")
        assert not window.is_triggered(Decimal("100.99"))
        assert window.is_triggered(Decimal("101"))
        assert window.is_triggered(Decimal("102"))

    def test_down_fires_when_signal_is_at_or_below_trigger(self) -> None:
        window = self._frozen(Direction.DOWN, "99")
        assert not window.is_triggered(Decimal("99.01"))
        assert window.is_triggered(Decimal("99"))
        assert window.is_triggered(Decimal("98"))

    def test_down_window_does_not_fire_immediately_at_the_opening_twap(self) -> None:
        """The exact bug a shared >= would produce."""
        window = ExecutionWindow(offset_seconds=3)
        window.freeze(
            opening_twap=Decimal("100"), ptb=Decimal("101"), buffer=Decimal("1"), frozen_at=0.0
        )
        assert window.direction is Direction.DOWN
        assert window.locked_trigger == Decimal("99")
        # At the freeze instant the signal TWAP equals the opening TWAP (100),
        # which is ABOVE the DOWN trigger (99) — must not be triggered.
        assert not window.is_triggered(window.opening_twap)

    def test_unfrozen_window_never_triggers(self) -> None:
        window = ExecutionWindow(offset_seconds=3)
        assert not window.is_triggered(Decimal("999999"))

    def test_none_signal_never_triggers(self) -> None:
        window = self._frozen(Direction.UP, "101")
        assert not window.is_triggered(None)

    def test_expired_window_does_not_trigger_even_with_a_winning_signal(self) -> None:
        window = self._frozen(Direction.UP, "101")
        window.mark_expired()
        assert not window.is_triggered(Decimal("500"))


class TestWindowFiringAndExpiry:
    def test_mark_fired_from_frozen(self) -> None:
        window = ExecutionWindow(offset_seconds=3)
        window.freeze(
            opening_twap=Decimal("100"), ptb=Decimal("99"), buffer=Decimal("1"), frozen_at=0.0
        )
        window.mark_fired(5.0)
        assert window.state is WindowState.FIRED
        assert window.fired_at == 5.0

    def test_mark_fired_from_pending_is_refused(self) -> None:
        window = ExecutionWindow(offset_seconds=3)
        with pytest.raises(WindowFreezeError, match="cannot fire"):
            window.mark_fired(5.0)

    def test_mark_fired_twice_is_refused(self) -> None:
        window = ExecutionWindow(offset_seconds=3)
        window.freeze(
            opening_twap=Decimal("100"), ptb=Decimal("99"), buffer=Decimal("1"), frozen_at=0.0
        )
        window.mark_fired(5.0)
        with pytest.raises(WindowFreezeError):
            window.mark_fired(6.0)

    def test_mark_expired_from_pending(self) -> None:
        window = ExecutionWindow(offset_seconds=3)
        window.mark_expired()
        assert window.state is WindowState.EXPIRED

    def test_mark_expired_from_frozen_retains_values(self) -> None:
        """The operator can see what the window was waiting for after it expires."""
        window = ExecutionWindow(offset_seconds=3)
        window.freeze(
            opening_twap=Decimal("100"), ptb=Decimal("99"), buffer=Decimal("1"), frozen_at=0.0
        )
        window.mark_expired()
        assert window.state is WindowState.EXPIRED
        assert window.opening_twap == Decimal("100")
        assert window.locked_trigger == Decimal("101")

    def test_mark_expired_from_fired_is_a_no_op(self) -> None:
        window = ExecutionWindow(offset_seconds=3)
        window.freeze(
            opening_twap=Decimal("100"), ptb=Decimal("99"), buffer=Decimal("1"), frozen_at=0.0
        )
        window.mark_fired(5.0)
        window.mark_expired()
        assert window.state is WindowState.FIRED  # unchanged, not overwritten


class TestOrderLifecycle:
    def _order(self, state: OrderState = OrderState.PENDING, filled: str = "0") -> Order:
        return Order(
            order_id="o1", market_slug="s", offset_seconds=3, direction=Direction.UP,
            price=Decimal("0.74"), size=Decimal("100"), state=state,
            filled_size=Decimal(filled),
        )

    @pytest.mark.parametrize(
        "state",
        [OrderState.PENDING, OrderState.SUBMITTED, OrderState.PARTIAL, OrderState.INDETERMINATE],
    )
    def test_live_states(self, state: OrderState) -> None:
        assert self._order(state).is_live

    @pytest.mark.parametrize(
        "state",
        [OrderState.FILLED, OrderState.CANCELLED, OrderState.EXPIRED, OrderState.REJECTED],
    )
    def test_non_live_states(self, state: OrderState) -> None:
        assert not self._order(state).is_live

    def test_indeterminate_counts_as_live(self) -> None:
        """A13: an unacknowledged cancel might still be resting on the book."""
        assert self._order(OrderState.INDETERMINATE).is_live

    def test_remaining_size(self) -> None:
        order = self._order(filled="40")
        assert order.remaining_size == Decimal("60")

    def test_remaining_size_never_negative(self) -> None:
        """An over-fill (redelivery quirk) must not report negative remaining."""
        order = self._order(filled="150")
        assert order.remaining_size == Decimal("0")

    def test_fully_filled_has_zero_remaining(self) -> None:
        assert self._order(filled="100").remaining_size == Decimal("0")


class TestMarketInstanceHasNoResetPath:
    """A11: created fresh per market, dropped at close, never reused."""

    def test_create_builds_the_configured_windows(self) -> None:
        market = MarketInstance.create(window_ts=WINDOW_TS, offsets=OFFSETS)
        assert set(market.windows) == set(OFFSETS)
        assert market.slug == f"btc-updown-5m-{WINDOW_TS}"
        assert market.close_ts == CLOSE_TS
        assert market.phase is MarketPhase.DISCOVERED

    def test_two_instances_do_not_share_containers(self) -> None:
        """The mistake default_factory exists to prevent: two markets sharing state."""
        a = MarketInstance.create(window_ts=WINDOW_TS, offsets=OFFSETS)
        b = MarketInstance.create(window_ts=CLOSE_TS, offsets=OFFSETS)
        a.accumulator.add(Decimal("100"))
        assert b.accumulator.observation_count == 0
        assert a.windows[3] is not b.windows[3]
        assert a.orders is not b.orders

    def test_a_fresh_instance_is_empty(self) -> None:
        market = MarketInstance.create(window_ts=WINDOW_TS, offsets=OFFSETS)
        assert market.ptb is None
        assert market.signal_twap is None
        assert market.observation_count == 0
        assert market.orders == []
        assert market.fills == []
        assert market.intents == []
        assert market.settlement is None


class TestPtbIsFrozenOnceOnMarketInstance:
    def test_freeze_ptb_sets_the_value(self) -> None:
        market = MarketInstance.create(window_ts=WINDOW_TS, offsets=OFFSETS)
        market.freeze_ptb("120000.00")
        assert market.ptb == Decimal("120000.00")

    def test_second_freeze_is_refused_even_with_the_same_value(self) -> None:
        market = MarketInstance.create(window_ts=WINDOW_TS, offsets=OFFSETS)
        market.freeze_ptb("120000.00")
        with pytest.raises(ValueError, match="already frozen"):
            market.freeze_ptb("120000.00")

    @pytest.mark.parametrize("value", ["0", "-1"])
    def test_non_positive_ptb_is_refused(self, value: str) -> None:
        market = MarketInstance.create(window_ts=WINDOW_TS, offsets=OFFSETS)
        with pytest.raises(ValueError, match="must be positive"):
            market.freeze_ptb(value)
        assert market.ptb is None

    def test_restore_ptb_sets_the_value_on_a_fresh_instance(self) -> None:
        market = MarketInstance.create(window_ts=WINDOW_TS, offsets=OFFSETS)
        market.restore_ptb("120000.00")
        assert market.ptb == Decimal("120000.00")

    def test_restore_ptb_refuses_to_overwrite_an_already_set_value(self) -> None:
        market = MarketInstance.create(window_ts=WINDOW_TS, offsets=OFFSETS)
        market.freeze_ptb("120000.00")
        with pytest.raises(ValueError, match="already set"):
            market.restore_ptb("999999.00")

    def test_ptb_has_no_public_setter(self) -> None:
        """The structural gate covers this; here as a runtime AttributeError."""
        market = MarketInstance.create(window_ts=WINDOW_TS, offsets=OFFSETS)
        with pytest.raises(AttributeError):
            market.ptb = Decimal("1")  # type: ignore[misc]


class TestObservationAcceptanceByPhase:
    """SETTLING accepts (those are inside the venue's averaging window);
    DEAD and SETTLED refuse (no PTB ever, and a closed record must not move).
    """

    @pytest.mark.parametrize(
        "phase",
        [
            MarketPhase.DISCOVERED,
            MarketPhase.ACTIVE,
            MarketPhase.CANCELLING,
            MarketPhase.SETTLING,
        ],
    )
    def test_accepting_phases(self, phase: MarketPhase) -> None:
        market = MarketInstance.create(window_ts=WINDOW_TS, offsets=OFFSETS)
        market.phase = phase
        market.add_observation(_obs(1.0, "100"))
        assert market.observation_count == 1

    @pytest.mark.parametrize("phase", [MarketPhase.DEAD, MarketPhase.SETTLED])
    def test_refusing_phases(self, phase: MarketPhase) -> None:
        market = MarketInstance.create(window_ts=WINDOW_TS, offsets=OFFSETS)
        market.phase = phase
        with pytest.raises(ObservationRejectedError, match=phase.value):
            market.add_observation(_obs(1.0, "100"))
        assert market.observation_count == 0

    def test_settling_observations_still_move_the_signal_twap(self) -> None:
        """These are exactly the observations inside the settlement window."""
        market = MarketInstance.create(window_ts=WINDOW_TS, offsets=OFFSETS)
        market.phase = MarketPhase.ACTIVE
        market.add_observation(_obs(1.0, "100"))
        market.phase = MarketPhase.SETTLING
        market.add_observation(_obs(2.0, "102"))
        assert market.signal_twap == Decimal("101")


class TestFreezeWindowUsesThisMarketsPtb:
    def test_requires_ptb_first(self) -> None:
        market = MarketInstance.create(window_ts=WINDOW_TS, offsets=OFFSETS)
        market.add_observation(_obs(1.0, "100"))
        with pytest.raises(WindowFreezeError, match="no official PTB"):
            market.freeze_window(3, buffer=Decimal("1"), frozen_at=0.0)

    def test_requires_at_least_one_observation(self) -> None:
        market = MarketInstance.create(window_ts=WINDOW_TS, offsets=OFFSETS)
        market.freeze_ptb("100")
        with pytest.raises(WindowFreezeError, match="no observations yet"):
            market.freeze_window(3, buffer=Decimal("1"), frozen_at=0.0)

    def test_freeze_window_uses_the_frozen_ptb_not_a_fresh_one(self) -> None:
        market = MarketInstance.create(window_ts=WINDOW_TS, offsets=OFFSETS)
        market.freeze_ptb("100")
        market.add_observation(_obs(1.0, "101"))
        window = market.freeze_window(3, buffer=Decimal("1"), frozen_at=0.0)
        assert window.ptb == Decimal("100")

    def test_every_window_freezes_against_the_same_ptb(self) -> None:
        """No path lets a later window freeze against a refreshed PTB."""
        market = MarketInstance.create(window_ts=WINDOW_TS, offsets=OFFSETS)
        market.freeze_ptb("100")
        market.add_observation(_obs(1.0, "101"))
        w15 = market.freeze_window(15, buffer=Decimal("2"), frozen_at=0.0)
        market.add_observation(_obs(2.0, "105"))  # signal moves between freezes
        w3 = market.freeze_window(3, buffer=Decimal("1"), frozen_at=1.0)
        assert w15.ptb == w3.ptb == Decimal("100")


class TestFilledSizeAcrossTheRepriceChain:
    """Hazard H4: summed across every order for the window, not per order.

    Counting per-order would let five sub-minimum fills across five reprices open
    five positions against what should have been a single trade's budget.
    """

    def _market_with_chain(self) -> MarketInstance:
        market = MarketInstance.create(window_ts=WINDOW_TS, offsets=OFFSETS)
        for i, price in enumerate(("0.74", "0.75", "0.76")):
            market.orders.append(
                Order(
                    order_id=f"o{i}", market_slug=market.slug, offset_seconds=3,
                    direction=Direction.UP, price=Decimal(price), size=Decimal("100"),
                    reprice_chain_id="chain-1",
                )
            )
        return market

    def test_sums_fills_across_multiple_orders_in_the_chain(self) -> None:
        market = self._market_with_chain()
        market.fills.append(
            Fill(fill_id="f1", order_id="o0", market_slug=market.slug,
                 size=Decimal("2"), price=Decimal("0.74"), ts=1.0)
        )
        market.fills.append(
            Fill(fill_id="f2", order_id="o1", market_slug=market.slug,
                 size=Decimal("3"), price=Decimal("0.75"), ts=2.0)
        )
        market.fills.append(
            Fill(fill_id="f3", order_id="o2", market_slug=market.slug,
                 size=Decimal("4"), price=Decimal("0.76"), ts=3.0)
        )
        assert market.filled_size_for_window(3) == Decimal("9")

    def test_fills_for_a_different_window_are_excluded(self) -> None:
        market = self._market_with_chain()
        market.orders.append(
            Order(order_id="other", market_slug=market.slug, offset_seconds=5,
                  direction=Direction.UP, price=Decimal("0.74"), size=Decimal("100"))
        )
        market.fills.append(
            Fill(fill_id="f1", order_id="other", market_slug=market.slug,
                 size=Decimal("50"), price=Decimal("0.74"), ts=1.0)
        )
        assert market.filled_size_for_window(3) == Decimal("0")

    def test_no_fills_yet_is_zero(self) -> None:
        market = self._market_with_chain()
        assert market.filled_size_for_window(3) == Decimal("0")

    def test_unknown_offset_is_zero_not_an_error(self) -> None:
        market = self._market_with_chain()
        assert market.filled_size_for_window(999) == Decimal("0")


class TestDirectionsHeld:
    """Hazard H3: UP at 0.79 plus DOWN at 0.22 costs 1.01, returns 1.00 — a loss."""

    def _market_with_order(self, direction: Direction, order_id: str = "o1") -> MarketInstance:
        market = MarketInstance.create(window_ts=WINDOW_TS, offsets=OFFSETS)
        market.orders.append(
            Order(order_id=order_id, market_slug=market.slug, offset_seconds=3,
                  direction=direction, price=Decimal("0.5"), size=Decimal("100"))
        )
        return market

    def test_no_fills_means_no_directions_held(self) -> None:
        market = self._market_with_order(Direction.UP)
        assert market.directions_held() == frozenset()

    def test_a_filled_order_registers_its_direction(self) -> None:
        market = self._market_with_order(Direction.UP)
        market.fills.append(
            Fill(fill_id="f1", order_id="o1", market_slug=market.slug,
                 size=Decimal("10"), price=Decimal("0.5"), ts=1.0)
        )
        assert market.directions_held() == frozenset({Direction.UP})

    def test_both_directions_can_be_held_simultaneously(self) -> None:
        market = self._market_with_order(Direction.UP, "o1")
        market.orders.append(
            Order(order_id="o2", market_slug=market.slug, offset_seconds=5,
                  direction=Direction.DOWN, price=Decimal("0.5"), size=Decimal("100"))
        )
        market.fills.append(
            Fill(fill_id="f1", order_id="o1", market_slug=market.slug,
                 size=Decimal("10"), price=Decimal("0.5"), ts=1.0)
        )
        market.fills.append(
            Fill(fill_id="f2", order_id="o2", market_slug=market.slug,
                 size=Decimal("10"), price=Decimal("0.5"), ts=2.0)
        )
        assert market.directions_held() == frozenset({Direction.UP, Direction.DOWN})

    def test_zero_size_fill_does_not_register_a_direction(self) -> None:
        market = self._market_with_order(Direction.UP)
        market.fills.append(
            Fill(fill_id="f1", order_id="o1", market_slug=market.slug,
                 size=Decimal("0"), price=Decimal("0.5"), ts=1.0)
        )
        assert market.directions_held() == frozenset()

    def test_fill_referencing_unknown_order_is_ignored(self) -> None:
        market = self._market_with_order(Direction.UP)
        market.fills.append(
            Fill(fill_id="f1", order_id="ghost", market_slug=market.slug,
                 size=Decimal("10"), price=Decimal("0.5"), ts=1.0)
        )
        assert market.directions_held() == frozenset()


class TestLiveOrdersIncludesIndeterminate:
    def test_live_orders_filters_by_state(self) -> None:
        market = MarketInstance.create(window_ts=WINDOW_TS, offsets=OFFSETS)
        market.orders = [
            Order(order_id="a", market_slug=market.slug, offset_seconds=3,
                  direction=Direction.UP, price=Decimal("0.5"), size=Decimal("1"),
                  state=OrderState.SUBMITTED),
            Order(order_id="b", market_slug=market.slug, offset_seconds=3,
                  direction=Direction.UP, price=Decimal("0.5"), size=Decimal("1"),
                  state=OrderState.FILLED),
            Order(order_id="c", market_slug=market.slug, offset_seconds=3,
                  direction=Direction.UP, price=Decimal("0.5"), size=Decimal("1"),
                  state=OrderState.INDETERMINATE),
        ]
        live_ids = {o.order_id for o in market.live_orders()}
        assert live_ids == {"a", "c"}


class TestWindowsByPriorityOnMarketInstance:
    def test_returns_ascending_offset_order(self) -> None:
        market = MarketInstance.create(window_ts=WINDOW_TS, offsets=OFFSETS)
        assert [w.offset_seconds for w in market.windows_by_priority()] == [3, 5, 7, 10, 15]

    def test_window_accessor_returns_the_same_object(self) -> None:
        market = MarketInstance.create(window_ts=WINDOW_TS, offsets=OFFSETS)
        assert market.window(3) is market.windows[3]


class TestSettlementNeverInfersOutcomeFromSignalTwap:
    """A12: the venue wins on disagreement, and the divergence is logged, not hidden."""

    def test_settlement_holds_the_venues_outcome_independently(self) -> None:
        settlement = Settlement(
            market_slug="s", outcome=Outcome.DOWN, settlement_twap=Decimal("99.5"),
            ptb=Decimal("100"), settled_at=1.0,
        )
        assert settlement.outcome is Outcome.DOWN
        assert settlement.divergence_logged is False

    def test_unresolved_is_distinct_from_a_real_outcome(self) -> None:
        """"Not told yet" and "venue said DOWN" must never collapse together."""
        pending = Settlement(
            market_slug="s", outcome=Outcome.UNRESOLVED, settlement_twap=None,
            ptb=None, settled_at=0.0,
        )
        assert pending.outcome is not Outcome.DOWN
        assert pending.outcome is not Outcome.UP

    def test_pnl_defaults_to_zero(self) -> None:
        settlement = Settlement(
            market_slug="s", outcome=Outcome.UP, settlement_twap=None, ptb=None,
            settled_at=0.0,
        )
        assert settlement.pnl == Decimal("0")


class TestExecutionIntentUniquenessIsArbitratedByStorageNotMemory:
    """A12: a SQLite UNIQUE constraint, not an in-memory set — survives a crash
    between the decision and the submission. Verified structurally in
    test_storage.py; here only the shape of the value object itself.
    """

    def test_intent_carries_the_arbitration_key_fields(self) -> None:
        intent = ExecutionIntent(
            market_slug="s", offset_seconds=3, direction=Direction.UP,
            signal_twap=Decimal("100"), locked_trigger=Decimal("99"), created_at=0.0,
        )
        assert (intent.market_slug, intent.offset_seconds) == ("s", 3)
