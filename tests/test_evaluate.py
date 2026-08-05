"""Trigger evaluation. The operators are NOT interchangeable, and this file proves it.

The centrepiece is TestSharedGreaterEqualDeletesTheStrategy. It builds a DOWN window,
applies the WRONG comparison to it in the test itself, and shows that the window fires
instantly with the TWAP unchanged — no BTC move, no crossing, nothing. That is not a
biased strategy, it is a deleted one: half of every market becomes "trade DOWN
immediately", and it looks perfectly healthy from every log line.

The wrong comparison is applied to a local copy of the rule, never by editing
arc/domain/models.py or arc/windows/evaluate.py. Mutating production code to prove a
gate is exactly the thing the standing constraint forbids.
"""

from __future__ import annotations

import logging
from decimal import Decimal

import pytest
from conftest import OFFSETS, VALID_TRADING_VALUES, WINDOW_TS

from arc.config import TradingConfig, build_trading_config
from arc.domain.enums import Direction, MarketPhase, WindowState
from arc.domain.models import ExecutionWindow, MarketInstance, Observation
from arc.storage.store import Store
from arc.windows.evaluate import evaluate_market, evaluate_window, is_satisfied
from arc.windows.freeze import freeze_window

NOW = float(WINDOW_TS + 290)


def _trading() -> TradingConfig:
    return build_trading_config(dict(VALID_TRADING_VALUES))


def _market(store: Store, *, ptb: str, price: str) -> MarketInstance:
    """A market whose signal TWAP is exactly `price` (one observation)."""
    market = MarketInstance.create(WINDOW_TS, OFFSETS)
    store.create_market(market, float(WINDOW_TS))
    market.phase = MarketPhase.ACTIVE
    market.freeze_ptb(ptb)
    market.add_observation(Observation(ts=float(WINDOW_TS), price=Decimal(price)))
    return market


def _set_twap(market: MarketInstance, value: str) -> None:
    """Force the signal TWAP to an exact value without touching frozen windows.

    Replaces the accumulator with a single-observation one, which is the honest way to
    say "the mean is now X" — the mean is computed on read from sum/count (hazard H1),
    so a one-observation accumulator of X has mean exactly X.
    """
    from arc.domain.models import TwapAccumulator

    market.accumulator = TwapAccumulator.restore(value, 1)


def _frozen(store: Store, *, ptb: str, price: str, offset: int = 10) -> tuple[MarketInstance, ExecutionWindow]:
    market = _market(store, ptb=ptb, price=price)
    window = market.window(offset)
    freeze_window(market, window, trading=_trading(), store=store, now=NOW)
    return market, window


class TestUpBoundary:
    """UP fires at >= locked_trigger. Equality fires."""

    def test_below_the_trigger_does_not_fire(self, store: Store) -> None:
        market, window = _frozen(store, ptb="64000.00", price="64100.00")
        assert window.direction is Direction.UP
        assert window.locked_trigger == Decimal("64102.00")
        _set_twap(market, "64101.99")
        result = evaluate_window(market, window, store=store, now=NOW)
        assert not result.fired
        assert window.state is WindowState.FROZEN

    def test_exactly_at_the_trigger_fires(self, store: Store) -> None:
        market, window = _frozen(store, ptb="64000.00", price="64100.00")
        _set_twap(market, "64102.00")
        assert evaluate_window(market, window, store=store, now=NOW).fired
        assert window.state is WindowState.FIRED

    def test_above_the_trigger_fires(self, store: Store) -> None:
        market, window = _frozen(store, ptb="64000.00", price="64100.00")
        _set_twap(market, "64102.01")
        assert evaluate_window(market, window, store=store, now=NOW).fired

    def test_one_cent_is_the_whole_difference(self, store: Store) -> None:
        """Decimal, so 64101.99 vs 64102.00 is decided exactly (criterion 20)."""
        market, window = _frozen(store, ptb="64000.00", price="64100.00")
        _set_twap(market, "64101.99")
        assert not is_satisfied(window, market.signal_twap)
        _set_twap(market, "64102.00")
        assert is_satisfied(window, market.signal_twap)


class TestDownBoundary:
    """DOWN fires at <= locked_trigger. The trigger sits BELOW the opening TWAP."""

    def test_the_trigger_is_below_the_opening_twap(self, store: Store) -> None:
        _, window = _frozen(store, ptb="64000.00", price="63900.00")
        assert window.direction is Direction.DOWN
        assert window.locked_trigger == Decimal("63898.00")
        assert window.locked_trigger < window.opening_twap  # type: ignore[operator]

    def test_above_the_trigger_does_not_fire(self, store: Store) -> None:
        market, window = _frozen(store, ptb="64000.00", price="63900.00")
        _set_twap(market, "63898.01")
        assert not evaluate_window(market, window, store=store, now=NOW).fired
        assert window.state is WindowState.FROZEN

    def test_exactly_at_the_trigger_fires(self, store: Store) -> None:
        market, window = _frozen(store, ptb="64000.00", price="63900.00")
        _set_twap(market, "63898.00")
        assert evaluate_window(market, window, store=store, now=NOW).fired

    def test_below_the_trigger_fires(self, store: Store) -> None:
        market, window = _frozen(store, ptb="64000.00", price="63900.00")
        _set_twap(market, "63897.99")
        assert evaluate_window(market, window, store=store, now=NOW).fired

    def test_a_down_window_does_not_fire_at_its_own_freeze_instant(self, store: Store) -> None:
        """The single most important assertion in this file.

        At the freeze instant signal_twap == opening_twap, which is one whole buffer
        ABOVE a DOWN trigger. With the correct `<=` the window sits and waits. With a
        shared `>=` it fires here, immediately, on every DOWN window ever frozen.
        """
        market, window = _frozen(store, ptb="64000.00", price="63900.00")
        assert market.signal_twap == window.opening_twap
        assert not evaluate_window(market, window, store=store, now=NOW).fired
        assert window.state is WindowState.FROZEN


class TestSharedGreaterEqualDeletesTheStrategy:
    """Criterion 5's regression test: prove `>=`-only breaks DOWN.

    The wrong rule is written HERE, as a local function, and applied to a correctly
    frozen window. Nothing in arc/ is modified.
    """

    @staticmethod
    def _wrong_shared_ge(window: ExecutionWindow, signal_twap: Decimal) -> bool:
        """The defect: one comparison for both directions."""
        assert window.locked_trigger is not None
        return signal_twap >= window.locked_trigger

    def test_the_wrong_rule_fires_every_down_window_immediately(self, store: Store) -> None:
        market, window = _frozen(store, ptb="64000.00", price="63900.00")
        twap = market.signal_twap
        assert twap is not None
        # The wrong rule: fires with the TWAP untouched, at the freeze instant.
        assert self._wrong_shared_ge(window, twap)
        # The real rule: does not.
        assert not is_satisfied(window, twap)

    def test_the_wrong_rule_fires_even_as_btc_moves_the_wrong_way(self, store: Store) -> None:
        """It is unconditional, which is why nothing reports it.

        BTC rising is the OPPOSITE of what a DOWN window is waiting for, and the wrong
        rule fires harder the more wrong the move gets.
        """
        market, window = _frozen(store, ptb="64000.00", price="63900.00")
        for price in ("63900.00", "64500.00", "70000.00"):
            _set_twap(market, price)
            twap = market.signal_twap
            assert twap is not None
            assert self._wrong_shared_ge(window, twap)
            assert not is_satisfied(window, twap)

    def test_the_wrong_rule_agrees_with_the_real_one_on_up_windows(self, store: Store) -> None:
        """Which is precisely why the defect is invisible: half the tests still pass."""
        market, window = _frozen(store, ptb="64000.00", price="64100.00")
        for price in ("64000.00", "64102.00", "64200.00"):
            _set_twap(market, price)
            twap = market.signal_twap
            assert twap is not None
            assert self._wrong_shared_ge(window, twap) == is_satisfied(window, twap)

    def test_and_the_real_rule_does_fire_a_down_window_that_actually_crosses(
        self, store: Store
    ) -> None:
        """The correct rule is not merely stricter — it still fires on a real signal."""
        market, window = _frozen(store, ptb="64000.00", price="63900.00")
        _set_twap(market, "63897.00")
        assert is_satisfied(window, market.signal_twap)


class TestUnfrozenWindowsNeverTrigger:
    def test_a_pending_window_never_fires(self, store: Store) -> None:
        market = _market(store, ptb="64000.00", price="64000.00")
        window = market.window(10)
        assert window.state is WindowState.PENDING
        _set_twap(market, "99999.00")
        result = evaluate_window(market, window, store=store, now=NOW)
        assert not result.fired
        assert not result.error  # not an error: the window simply has not activated
        assert window.state is WindowState.PENDING

    def test_is_satisfied_is_false_for_a_pending_window(self, store: Store) -> None:
        market = _market(store, ptb="64000.00", price="64000.00")
        assert not is_satisfied(market.window(10), Decimal("99999.00"))

    def test_a_window_with_no_twap_never_fires(self, store: Store) -> None:
        """No observations means no mean. A None TWAP is not a crossing."""
        _, window = _frozen(store, ptb="64000.00", price="63900.00")
        assert not is_satisfied(window, None)


class TestFiresOnlyOnce:
    """Criterion 12."""

    def test_repeated_evaluation_fires_exactly_once(self, store: Store) -> None:
        market, window = _frozen(store, ptb="64000.00", price="64100.00")
        _set_twap(market, "64200.00")
        fires = sum(
            1
            for _ in range(20)
            if evaluate_window(market, window, store=store, now=NOW).fired
        )
        assert fires == 1

    def test_a_fired_window_is_skipped_silently(self, store: Store) -> None:
        market, window = _frozen(store, ptb="64000.00", price="64100.00")
        _set_twap(market, "64200.00")
        evaluate_window(market, window, store=store, now=NOW)
        result = evaluate_window(market, window, store=store, now=NOW + 1)
        assert not result.fired
        assert not result.error

    def test_an_expired_window_never_fires(self, store: Store) -> None:
        market, window = _frozen(store, ptb="64000.00", price="64100.00")
        window.state = WindowState.EXPIRED
        _set_twap(market, "64200.00")
        assert not evaluate_window(market, window, store=store, now=NOW).fired
        assert window.state is WindowState.EXPIRED

    def test_the_fire_is_persisted_before_the_call_returns(self, store: Store) -> None:
        """A crash immediately after must not let the window fire a second time."""
        market, window = _frozen(store, ptb="64000.00", price="64100.00")
        _set_twap(market, "64200.00")
        assert evaluate_window(market, window, store=store, now=NOW).fired
        row = store.restore_frozen(market.slug, 10)
        assert row is not None
        assert row["state"] is WindowState.FIRED
        assert row["fired_at"] == NOW


class TestSimultaneousUpAndDown:
    """Criterion 6: one market may hold a DOWN 10s and an UP 3s at the same time."""

    def _split_market(self, store: Store) -> MarketInstance:
        """Freeze 10s while the TWAP is below PTB, then 3s while it is above."""
        market = _market(store, ptb="64000.00", price="63900.00")
        trading = _trading()
        freeze_window(market, market.window(10), trading=trading, store=store, now=NOW)
        _set_twap(market, "64100.00")
        freeze_window(market, market.window(3), trading=trading, store=store, now=NOW + 7)
        return market

    def test_the_two_windows_hold_opposite_directions(self, store: Store) -> None:
        market = self._split_market(store)
        assert market.window(10).direction is Direction.DOWN
        assert market.window(3).direction is Direction.UP

    def test_each_holds_its_own_trigger_and_its_own_buffer(self, store: Store) -> None:
        market = self._split_market(store)
        assert market.window(10).locked_trigger == Decimal("63898.00")  # 63900 - 2.00
        assert market.window(3).locked_trigger == Decimal("64101.00")  # 64100 + 1.00

    def test_the_up_window_can_fire_while_the_down_window_waits(self, store: Store) -> None:
        market = self._split_market(store)
        _set_twap(market, "64101.00")
        results = {r.offset_seconds: r for r in evaluate_market(market, store=store, now=NOW)}
        assert results[3].fired
        assert not results[10].fired
        assert market.window(10).state is WindowState.FROZEN

    def test_the_down_window_can_fire_while_the_up_window_waits(self, store: Store) -> None:
        market = self._split_market(store)
        _set_twap(market, "63898.00")
        results = {r.offset_seconds: r for r in evaluate_market(market, store=store, now=NOW)}
        assert results[10].fired
        assert not results[3].fired

    def test_no_twap_value_satisfies_both_at_once(self, store: Store) -> None:
        """They are genuinely independent, not two views of one condition."""
        market = self._split_market(store)
        for price in ("63000.00", "63898.00", "64000.00", "64101.00", "65000.00"):
            _set_twap(market, price)
            twap = market.signal_twap
            both = is_satisfied(market.window(10), twap) and is_satisfied(
                market.window(3), twap
            )
            assert not both, price


class TestEvaluationOrder:
    """Criterion 7: 3 -> 5 -> 7 -> 10 -> 15."""

    def test_results_come_back_in_priority_order(self, store: Store) -> None:
        market = _market(store, ptb="64000.00", price="64100.00")
        trading = _trading()
        for offset in OFFSETS:
            freeze_window(market, market.window(offset), trading=trading, store=store, now=NOW)
        results = evaluate_market(market, store=store, now=NOW)
        assert [r.offset_seconds for r in results] == [3, 5, 7, 10, 15]

    def test_every_window_is_evaluated_even_after_one_fires(self, store: Store) -> None:
        """A 3s fire must not suppress the 10s window's entirely separate signal."""
        market = _market(store, ptb="64000.00", price="64100.00")
        trading = _trading()
        for offset in OFFSETS:
            freeze_window(market, market.window(offset), trading=trading, store=store, now=NOW)
        # Buffers: 3s=1.00, 5s=1.25, 7s=1.50, 10s=2.00, 15s=2.00 above 64100.
        _set_twap(market, "64101.50")
        results = evaluate_market(market, store=store, now=NOW)
        assert len(results) == len(OFFSETS)
        fired = {r.offset_seconds for r in results if r.fired}
        assert fired == {3, 5, 7}


class TestOneWindowsFailureDoesNotAffectAnother:
    """Criterion 18."""

    def test_a_raising_window_is_captured_and_the_rest_are_evaluated(
        self, store: Store, caplog: pytest.LogCaptureFixture
    ) -> None:
        market = _market(store, ptb="64000.00", price="64100.00")
        trading = _trading()
        for offset in OFFSETS:
            freeze_window(market, market.window(offset), trading=trading, store=store, now=NOW)
        _set_twap(market, "64200.00")

        # A window whose trigger comparison raises. Built by replacing ONE window object
        # with a subclass instance; no production code is edited.
        class ExplodingWindow(ExecutionWindow):
            def is_triggered(self, signal_twap: Decimal | None) -> bool:
                raise RuntimeError("boom")

        broken = ExplodingWindow(offset_seconds=7)
        broken.restore_frozen(
            opening_twap=Decimal("64100.00"),
            ptb=Decimal("64000.00"),
            buffer=Decimal("1.50"),
            direction=Direction.UP,
            locked_trigger=Decimal("64101.50"),
            frozen_at=NOW,
        )
        market.windows[7] = broken

        with caplog.at_level(logging.ERROR, logger="arc"):
            results = evaluate_market(
                market, store=store, now=NOW, logger=logging.getLogger("arc.test.evaluate")
            )
        by_offset = {r.offset_seconds: r for r in results}
        assert by_offset[7].error
        assert not by_offset[7].ok
        # The other four still fired.
        assert {o for o, r in by_offset.items() if r.fired} == {3, 5, 10, 15}


class TestDeterminism:
    """Criterion 16: same frozen values plus same TWAP gives the same verdict, always."""

    def test_the_comparison_is_pure(self, store: Store) -> None:
        _, window = _frozen(store, ptb="64000.00", price="64100.00")
        twap = Decimal("64102.00")
        assert len({is_satisfied(window, twap) for _ in range(100)}) == 1

    def test_the_clock_does_not_affect_the_verdict(self, store: Store) -> None:
        """`now` is recorded as fired_at and never compared. D1: no clock gate."""
        _, window = _frozen(store, ptb="64000.00", price="64100.00")
        twap = Decimal("64101.99")
        # Not satisfied, at any instant.
        assert not is_satisfied(window, twap)

    def test_a_result_cannot_be_edited(self, store: Store) -> None:
        market, window = _frozen(store, ptb="64000.00", price="64100.00")
        result = evaluate_window(market, window, store=store, now=NOW)
        with pytest.raises((AttributeError, TypeError)):
            result.fired = True  # type: ignore[misc]
