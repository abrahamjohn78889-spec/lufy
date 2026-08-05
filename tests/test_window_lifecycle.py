"""Window lifecycle: which transitions are legal, and NO_SIGNAL is not an error.

Criterion 14 (transitions are strictly monotonic and illegal ones are impossible) and
criterion 8 (a window that never crosses ends NO_SIGNAL, a normal terminal state, never
logged as an error).

Criterion 8 is worth a test of its own because the cheap implementation logs it at ERROR
"just in case", and then the log of a 24/7 process is full of errors for the single most
common outcome in the system — most windows never cross. Real errors become unfindable.

Criteria 11 and 17 (evaluation never blocks and is light enough for continuous
high-frequency processing) are covered at the bottom: the pass is synchronous by
construction, and the timing check bounds the per-observation cost.
"""

from __future__ import annotations

import logging
import time
from decimal import Decimal

import pytest
from conftest import OFFSETS, VALID_TRADING_VALUES, WINDOW_TS

from arc.config import TradingConfig, build_trading_config
from arc.domain.enums import Direction, MarketPhase, WindowState
from arc.domain.models import MarketInstance, Observation, TwapAccumulator
from arc.storage.store import Store
from arc.windows.engine import WindowEngine
from arc.windows.freeze import freeze_window
from arc.windows.lifecycle import (
    LEGAL_TRANSITIONS,
    TERMINAL_WINDOW_STATES,
    WindowTransitionError,
    assert_can_fire,
    expire_window,
    is_terminal,
    transition,
)

NOW = float(WINDOW_TS + 290)

ALL_STATES = (
    WindowState.PENDING,
    WindowState.FROZEN,
    WindowState.FIRED,
    WindowState.EXPIRED,
)


def _trading() -> TradingConfig:
    return build_trading_config(dict(VALID_TRADING_VALUES))


def _market(store: Store, *, ptb: str = "64000.00", price: str = "64100.00") -> MarketInstance:
    market = MarketInstance.create(WINDOW_TS, OFFSETS)
    store.create_market(market, float(WINDOW_TS))
    market.phase = MarketPhase.ACTIVE
    market.freeze_ptb(ptb)
    market.add_observation(Observation(ts=float(WINDOW_TS), price=Decimal(price)))
    return market


class TestTheTransitionGraph:
    def test_there_is_no_rejected_or_cancelled_state(self) -> None:
        """A rejected freeze leaves the window PENDING and untouched — structurally.

        WindowState has no REJECTED member, so "partial freeze is impossible" is a
        property of the type rather than of the code that must remember to check.
        """
        assert {s.value for s in WindowState} == {"PENDING", "FROZEN", "FIRED", "EXPIRED"}

    def test_terminal_states_have_no_outgoing_edges(self) -> None:
        for state in TERMINAL_WINDOW_STATES:
            assert LEGAL_TRANSITIONS[state] == ()

    def test_pending_is_reachable_from_nowhere(self) -> None:
        """Criterion 14: monotonic. Nothing may go back to PENDING."""
        for _state, targets in LEGAL_TRANSITIONS.items():
            assert WindowState.PENDING not in targets

    def test_is_terminal_matches_the_graph(self) -> None:
        for state in ALL_STATES:
            assert is_terminal(state) == (LEGAL_TRANSITIONS[state] == ())

    @pytest.mark.parametrize("start", ALL_STATES)
    @pytest.mark.parametrize("target", ALL_STATES)
    def test_every_illegal_transition_raises_and_changes_nothing(
        self, store: Store, start: WindowState, target: WindowState
    ) -> None:
        market = _market(store)
        window = market.window(10)
        window.state = start
        if target in LEGAL_TRANSITIONS[start]:
            transition(window, target)
            assert window.state is target
            return
        with pytest.raises(WindowTransitionError):
            transition(window, target)
        assert window.state is start

    def test_a_no_op_transition_is_refused(self, store: Store) -> None:
        """FROZEN -> FROZEN would mean a second freeze on a window that already has one."""
        market = _market(store)
        window = market.window(10)
        window.state = WindowState.FROZEN
        with pytest.raises(WindowTransitionError):
            transition(window, WindowState.FROZEN)


class TestAssertCanFire:
    def test_a_frozen_window_with_all_values_may_fire(self, store: Store) -> None:
        market = _market(store)
        window = market.window(10)
        freeze_window(market, window, trading=_trading(), store=store, now=NOW)
        assert_can_fire(window)  # does not raise

    def test_a_pending_window_may_not_fire(self, store: Store) -> None:
        market = _market(store)
        with pytest.raises(WindowTransitionError):
            assert_can_fire(market.window(10))

    def test_a_terminal_window_may_not_fire(self, store: Store) -> None:
        market = _market(store)
        window = market.window(10)
        freeze_window(market, window, trading=_trading(), store=store, now=NOW)
        window.state = WindowState.FIRED
        with pytest.raises(WindowTransitionError):
            assert_can_fire(window)

    def test_a_window_missing_a_trigger_may_not_fire(self, store: Store) -> None:
        """Defence in depth: even if a FROZEN state were reached without a trigger."""
        market = _market(store)
        window = market.window(10)
        window.state = WindowState.FROZEN
        window.direction = Direction.UP
        with pytest.raises(WindowTransitionError):
            assert_can_fire(window)

    def test_it_raises_rather_than_returning_false(self, store: Store) -> None:
        """A boolean that a caller can forget to check is not a guard."""
        market = _market(store)
        with pytest.raises(WindowTransitionError):
            assert_can_fire(market.window(10))


class TestNoSignalIsNormal:
    """Criterion 8."""

    def test_a_never_crossing_window_ends_expired(self, store: Store) -> None:
        market = _market(store)
        window = market.window(10)
        freeze_window(market, window, trading=_trading(), store=store, now=NOW)
        assert expire_window(window)
        assert window.state is WindowState.EXPIRED

    def test_it_is_never_logged_as_an_error(
        self, store: Store, caplog: pytest.LogCaptureFixture
    ) -> None:
        market = _market(store)
        window = market.window(10)
        freeze_window(market, window, trading=_trading(), store=store, now=NOW)
        with caplog.at_level(logging.DEBUG, logger="arc"):
            expire_window(window, logger=logging.getLogger("arc.test.lifecycle"))
        assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert any("Window No Signal" in r.getMessage() for r in caplog.records)

    def test_a_market_where_nothing_crosses_logs_no_error(
        self, store: Store, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The overwhelmingly common outcome. It must be quiet."""
        market = _market(store)
        trading = _trading()
        engine = WindowEngine(store, trading, logger=logging.getLogger("arc.test.lifecycle"))
        with caplog.at_level(logging.DEBUG, logger="arc"):
            engine.pass_over(market, float(WINDOW_TS + 299))
            expired = engine.expire_all(market)
        assert len(expired) == len(OFFSETS)
        assert not [r for r in caplog.records if r.levelno >= logging.WARNING]

    def test_expiring_an_already_terminal_window_returns_false(self, store: Store) -> None:
        market = _market(store)
        window = market.window(10)
        window.state = WindowState.FIRED
        assert not expire_window(window)
        assert window.state is WindowState.FIRED

    def test_a_pending_window_can_expire_directly(self, store: Store) -> None:
        """A market that died before its windows opened must still leave no orphan."""
        market = _market(store)
        window = market.window(10)
        assert expire_window(window)
        assert window.state is WindowState.EXPIRED

    def test_expire_all_leaves_nothing_pending(self, store: Store) -> None:
        """Criterion 19."""
        market = _market(store)
        trading = _trading()
        engine = WindowEngine(store, trading)
        engine.pass_over(market, float(WINDOW_TS + 292))  # opens 15, 10, 7, 5, 3? -> 15..8
        engine.expire_all(market)
        assert all(is_terminal(w.state) for w in market.windows_by_priority())
        for row in store.windows_for(market.slug):
            assert row["state"] in ("FIRED", "EXPIRED")

    def test_expire_all_preserves_a_fired_window(self, store: Store) -> None:
        market = _market(store)
        trading = _trading()
        engine = WindowEngine(store, trading)
        freeze_window(market, market.window(10), trading=trading, store=store, now=NOW)
        market.accumulator = TwapAccumulator.restore("64200.00", 1)
        assert engine.pass_over(market, NOW).fired == (10,)
        engine.expire_all(market)
        assert market.window(10).state is WindowState.FIRED


class TestEvaluationDoesNotBlock:
    """Criteria 11 and 17."""

    def test_a_pass_is_a_plain_synchronous_call(self, store: Store) -> None:
        """Not a coroutine, so there is nothing to await, cancel, or leave pending."""
        import inspect

        assert not inspect.iscoroutinefunction(WindowEngine.pass_over)
        assert not inspect.isasyncgenfunction(WindowEngine.pass_over)

    def test_a_pass_over_five_windows_costs_well_under_a_millisecond(
        self, store: Store
    ) -> None:
        """A loose bound, deliberately. The point is the order of magnitude.

        The feed delivers roughly one observation per second and each triggers a pass, so
        anything in the microsecond range is free. A bound this loose still catches the
        failure that matters: an accidental I/O call or a per-pass reload, which costs
        milliseconds, not microseconds.
        """
        market = _market(store)
        trading = _trading()
        engine = WindowEngine(store, trading)
        for offset in OFFSETS:
            freeze_window(market, market.window(offset), trading=trading, store=store, now=NOW)
        # None will fire: the TWAP sits one buffer below every UP trigger.
        iterations = 2000
        start = time.perf_counter()
        for _ in range(iterations):
            engine.pass_over(market, NOW)
        elapsed = time.perf_counter() - start
        per_pass_ms = elapsed * 1000 / iterations
        assert per_pass_ms < 1.0, f"{per_pass_ms:.3f} ms per pass over five windows"

    def test_no_pass_writes_unless_something_changes(self, store: Store) -> None:
        """No I/O on the hot path. A write per observation would be ~300 per market.

        Counted by wrapping the Store's write methods on an instance, not by editing
        arc/storage/store.py.
        """
        market = _market(store)
        trading = _trading()
        engine = WindowEngine(store, trading)
        for offset in OFFSETS:
            freeze_window(market, market.window(offset), trading=trading, store=store, now=NOW)

        calls = 0
        real_state = store.save_window_state
        real_frozen = store.save_window_frozen

        def counting_state(*args: object, **kwargs: object) -> None:
            nonlocal calls
            calls += 1
            return real_state(*args, **kwargs)  # type: ignore[arg-type]

        def counting_frozen(*args: object, **kwargs: object) -> bool:
            nonlocal calls
            calls += 1
            return real_frozen(*args, **kwargs)  # type: ignore[arg-type]

        store.save_window_state = counting_state  # type: ignore[method-assign]
        store.save_window_frozen = counting_frozen  # type: ignore[method-assign]
        try:
            for _ in range(100):
                engine.pass_over(market, NOW)
        finally:
            store.save_window_state = real_state  # type: ignore[method-assign]
            store.save_window_frozen = real_frozen  # type: ignore[method-assign]
        assert calls == 0, f"{calls} writes across 100 no-op passes"

    def test_one_windows_failure_does_not_block_the_pass(self, store: Store) -> None:
        """Criterion 18, at the engine level rather than the evaluator's."""
        market = _market(store, price="63900.00")
        trading = _trading()
        engine = WindowEngine(store, trading)
        # A window with no configured buffer: its freeze is rejected, and the other five
        # must still be driven on the same pass.
        from arc.domain.models import ExecutionWindow

        market.windows[99] = ExecutionWindow(offset_seconds=99)
        result = engine.pass_over(market, float(WINDOW_TS + 299))
        assert 99 not in result.frozen
        assert set(result.frozen) == set(OFFSETS)
        assert market.window(99).state is WindowState.PENDING
