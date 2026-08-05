"""Feed staleness: warn, block, recover without a restart.

The recovery tests are the point. A watchdog that latched would turn a two-second
network hiccup into a dead session, and the operator would learn to restart
reflexively — which loses the in-memory market and is worse than the hiccup.

TRAP 1 is asserted structurally at the end: nothing in this module may read
window_seconds, because the gap between updates says NOTHING about the length of the
settlement TWAP window.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from arc.clock import FrozenClock
from arc.market.watchdog import (
    HEALTH_BLOCKED,
    HEALTH_OK,
    HEALTH_WARN,
    FeedHealth,
    FeedWatchdog,
)


def _watchdog(clock: FrozenClock) -> FeedWatchdog:
    return FeedWatchdog(clock, warn_ms=3_000, critical_ms=10_000)


@pytest.fixture
def mono() -> FrozenClock:
    return FrozenClock(now=1_754_400_000.0, monotonic=100.0)


class TestConstruction:
    def test_thresholds_are_reported(self, mono: FrozenClock) -> None:
        watchdog = _watchdog(mono)
        assert watchdog.warn_ms == 3_000
        assert watchdog.critical_ms == 10_000

    def test_a_non_positive_warn_threshold_is_rejected(self, mono: FrozenClock) -> None:
        with pytest.raises(ValueError, match="warn_ms"):
            FeedWatchdog(mono, warn_ms=0, critical_ms=10_000)

    def test_a_critical_below_the_warn_is_rejected(self, mono: FrozenClock) -> None:
        """Otherwise the warning never fires before the block."""
        with pytest.raises(ValueError, match="must exceed"):
            FeedWatchdog(mono, warn_ms=10_000, critical_ms=3_000)

    def test_a_critical_equal_to_the_warn_is_rejected(self, mono: FrozenClock) -> None:
        with pytest.raises(ValueError, match="must exceed"):
            FeedWatchdog(mono, warn_ms=5_000, critical_ms=5_000)

    def test_the_health_namespace_matches_the_constants(self) -> None:
        assert FeedHealth.OK == HEALTH_OK
        assert FeedHealth.WARN == HEALTH_WARN
        assert FeedHealth.BLOCKED == HEALTH_BLOCKED


class TestInitialState:
    def test_a_watchdog_that_has_never_ticked_is_blocked(self, mono: FrozenClock) -> None:
        """Starting at OK would report a connection that was never established healthy."""
        watchdog = _watchdog(mono)
        assert watchdog.status == HEALTH_BLOCKED
        assert watchdog.blocked is True
        assert watchdog.has_ticked is False

    def test_the_age_is_none_before_the_first_tick(self, mono: FrozenClock) -> None:
        """None, not zero: no observation is not the same as a fresh observation."""
        assert _watchdog(mono).age_ms() is None

    def test_evaluating_before_any_tick_stays_blocked(self, mono: FrozenClock) -> None:
        assert _watchdog(mono).evaluate() == HEALTH_BLOCKED


class TestThresholds:
    def test_a_fresh_tick_is_ok(self, mono: FrozenClock) -> None:
        watchdog = _watchdog(mono)
        watchdog.tick()
        assert watchdog.evaluate() == HEALTH_OK
        assert watchdog.blocked is False

    def test_just_below_the_warn_threshold_is_still_ok(self, mono: FrozenClock) -> None:
        watchdog = _watchdog(mono)
        watchdog.tick()
        mono.advance(2.999)
        assert watchdog.evaluate() == HEALTH_OK

    def test_at_the_warn_threshold_the_operator_is_told(self, mono: FrozenClock) -> None:
        watchdog = _watchdog(mono)
        watchdog.tick()
        mono.advance(3.0)
        assert watchdog.evaluate() == HEALTH_WARN

    def test_warning_does_not_block_trading(self, mono: FrozenClock) -> None:
        watchdog = _watchdog(mono)
        watchdog.tick()
        mono.advance(5.0)
        watchdog.evaluate()
        assert watchdog.blocked is False

    def test_just_below_critical_is_still_only_a_warning(self, mono: FrozenClock) -> None:
        watchdog = _watchdog(mono)
        watchdog.tick()
        mono.advance(9.999)
        assert watchdog.evaluate() == HEALTH_WARN

    def test_at_the_critical_threshold_trading_is_blocked(self, mono: FrozenClock) -> None:
        watchdog = _watchdog(mono)
        watchdog.tick()
        mono.advance(10.0)
        assert watchdog.evaluate() == HEALTH_BLOCKED
        assert watchdog.blocked is True

    def test_the_age_is_measured_in_milliseconds(self, mono: FrozenClock) -> None:
        watchdog = _watchdog(mono)
        watchdog.tick()
        mono.advance(1.5)
        assert watchdog.age_ms() == pytest.approx(1_500.0)


class TestMonotonicClock:
    def test_staleness_is_measured_on_the_monotonic_reading(self) -> None:
        """An NTP step correction — chrony will apply these on the VPS — can move wall
        time backwards by seconds. A wall-clock watchdog would report a negative age or
        declare a healthy feed critically stale at the instant the correction landed.
        """
        clock = FrozenClock(now=1_754_400_000.0, monotonic=100.0)
        watchdog = _watchdog(clock)
        watchdog.tick()

        # Wall time steps backwards by a minute; monotonic does not move.
        clock.set(1_754_399_940.0)
        assert watchdog.age_ms() == pytest.approx(0.0)
        assert watchdog.evaluate() == HEALTH_OK

    def test_a_backward_wall_clock_step_never_moves_the_monotonic_age(self) -> None:
        """Repeated backward corrections still cannot produce a negative age."""
        clock = FrozenClock(now=1_754_400_000.0, monotonic=100.0)
        watchdog = _watchdog(clock)
        watchdog.tick()
        for _ in range(3):
            clock.set(clock.now() - 30.0)
        age = watchdog.age_ms()
        assert age is not None
        assert age >= 0.0
        assert watchdog.evaluate() == HEALTH_OK


class TestRecovery:
    def test_a_blocked_watchdog_recovers_when_data_returns(self, mono: FrozenClock) -> None:
        """No restart required. That is the entire reason this does not latch."""
        watchdog = _watchdog(mono)
        watchdog.tick()
        mono.advance(20.0)
        assert watchdog.evaluate() == HEALTH_BLOCKED

        watchdog.tick()
        assert watchdog.status == HEALTH_OK
        assert watchdog.evaluate() == HEALTH_OK

    def test_evaluating_repeatedly_never_improves_the_status(self, mono: FrozenClock) -> None:
        """evaluate() is a pure read of elapsed time; recovery happens through tick()."""
        watchdog = _watchdog(mono)
        watchdog.tick()
        mono.advance(20.0)
        for _ in range(5):
            assert watchdog.evaluate() == HEALTH_BLOCKED

    def test_recovery_and_relapse_are_both_counted_as_transitions(
        self, mono: FrozenClock
    ) -> None:
        watchdog = _watchdog(mono)
        watchdog.tick()  # BLOCKED -> OK
        mono.advance(20.0)
        watchdog.evaluate()  # OK -> BLOCKED
        watchdog.tick()  # BLOCKED -> OK
        assert watchdog.transitions == 3

    def test_a_status_that_does_not_change_is_not_a_transition(self, mono: FrozenClock) -> None:
        watchdog = _watchdog(mono)
        watchdog.tick()
        before = watchdog.transitions
        watchdog.tick()
        watchdog.evaluate()
        assert watchdog.transitions == before


class TestDisconnect:
    def test_a_dropped_connection_blocks_without_waiting_out_the_timer(
        self, mono: FrozenClock
    ) -> None:
        """The socket closing is direct evidence no data is coming. Waiting out
        critical_ms first leaves a window in which trading is permitted against a feed
        already known to be gone."""
        watchdog = _watchdog(mono)
        watchdog.tick()
        assert watchdog.status == HEALTH_OK
        assert watchdog.mark_disconnected() == HEALTH_BLOCKED
        assert watchdog.blocked is True

    def test_a_disconnect_clears_the_last_tick(self, mono: FrozenClock) -> None:
        watchdog = _watchdog(mono)
        watchdog.tick()
        watchdog.mark_disconnected()
        assert watchdog.age_ms() is None
        assert watchdog.has_ticked is False

    def test_a_reconnect_with_data_recovers_from_a_disconnect(self, mono: FrozenClock) -> None:
        watchdog = _watchdog(mono)
        watchdog.tick()
        watchdog.mark_disconnected()
        watchdog.tick()
        assert watchdog.evaluate() == HEALTH_OK


class TestPerInstanceState:
    def test_two_watchdogs_do_not_share_staleness(self, mono: FrozenClock) -> None:
        """A11: nothing per-feed at module scope."""
        first = _watchdog(mono)
        second = _watchdog(mono)
        first.tick()
        assert first.evaluate() == HEALTH_OK
        assert second.evaluate() == HEALTH_BLOCKED

    def test_two_watchdogs_do_not_share_transition_counts(self, mono: FrozenClock) -> None:
        first = _watchdog(mono)
        second = _watchdog(mono)
        first.tick()
        assert second.transitions == 0


class TestTrap1:
    """TRAP 1, asserted structurally.

    30s and 60s are LOOKBACK windows, not publication rates. This module measures
    the gap between updates only to answer "is data arriving", and a source-level
    check is what keeps a future change from quietly using that gap to infer or
    validate a window length — the payload's own declared field is the only
    admissible source, and it is read in settlement_feed.py.
    """

    @pytest.fixture
    def watchdog_source(self, source_root: Path) -> str:
        return (source_root / "arc" / "market" / "watchdog.py").read_text(encoding="utf-8")

    def test_the_module_never_reads_a_window_length(self, watchdog_source: str) -> None:
        tree = ast.parse(watchdog_source)
        names: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute):
                names.add(node.attr)
            elif isinstance(node, ast.Name):
                names.add(node.id)
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                names.add(node.value)
        for forbidden in ("window_seconds", "windowSeconds", "SETTLEMENT_WINDOW_SECONDS"):
            assert forbidden not in names, (
                f"watchdog.py references {forbidden} — update cadence must never be "
                "used to infer or check a TWAP window length (TRAP 1)"
            )

    def test_the_module_imports_no_settlement_window_constant(
        self, watchdog_source: str
    ) -> None:
        tree = ast.parse(watchdog_source)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                imported = {alias.name for alias in node.names}
                assert "SETTLEMENT_WINDOW_SECONDS" not in imported

    def test_no_thirty_or_sixty_second_literal_appears(self, watchdog_source: str) -> None:
        """The thresholds come from configuration. A hardcoded 30 or 60 here would be
        a window length leaking into a staleness policy."""
        tree = ast.parse(watchdog_source)
        literals = [
            node.value
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, int)
        ]
        assert 30 not in literals
        assert 60 not in literals
