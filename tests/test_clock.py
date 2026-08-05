"""Clock injection and drift monitoring.

Nothing in ARC calls time.time() directly (enforced structurally in
test_infrastructure.py); every timed component takes a Clock. FrozenClock is not a
mock — it is a real Clock implementation the test drives directly, which is what
lets a test place the process at exactly close_ts - 3.0s and assert what happens.

DriftMonitor classifies on the ABSOLUTE offset. That single design choice is
tested from several angles below because getting it backwards — treating a
negative offset as automatically safe — is the natural mistake, and it would mean
a local clock lagging the venue by 900ms reads as healthy while the 3-second
window is being evaluated with 2.1 real seconds left on it.
"""

from __future__ import annotations

import pytest

from arc.clock import Clock, DriftMonitor, DriftStatus, FrozenClock, SystemClock


class TestClockProtocol:
    def test_system_clock_satisfies_the_protocol(self) -> None:
        assert isinstance(SystemClock(), Clock)

    def test_frozen_clock_satisfies_the_protocol(self) -> None:
        assert isinstance(FrozenClock(now=0.0), Clock)

    def test_system_clock_now_advances_with_real_time(self) -> None:
        import time

        clock = SystemClock()
        first = clock.now()
        time.sleep(0.01)
        assert clock.now() > first

    def test_system_clock_monotonic_never_goes_backwards(self) -> None:
        clock = SystemClock()
        readings = [clock.monotonic() for _ in range(5)]
        assert readings == sorted(readings)


class TestFrozenClockOnlyMovesOnCommand:
    def test_now_does_not_drift_on_its_own(self) -> None:
        clock = FrozenClock(now=1754400000.0)
        first = clock.now()
        for _ in range(1000):
            pass
        assert clock.now() == first

    def test_advance_moves_wall_and_monotonic_together(self) -> None:
        clock = FrozenClock(now=1000.0, monotonic=0.0)
        clock.advance(5.0)
        assert clock.now() == 1005.0
        assert clock.monotonic() == 5.0

    def test_advance_can_be_fractional_and_repeated(self) -> None:
        clock = FrozenClock(now=0.0)
        clock.advance(0.3)
        clock.advance(0.3)
        clock.advance(0.4)
        assert clock.now() == pytest.approx(1.0)

    def test_jumping_straight_past_an_instant_is_the_whole_point(self) -> None:
        """The level-triggered activation tests in test_timing.py rely on this."""
        clock = FrozenClock(now=1754400297.0)  # close_ts - 3
        clock.advance(1.2)  # skips clean over the exact activation instant
        assert clock.now() == pytest.approx(1754400298.2)

    def test_set_jumps_wall_time_to_an_absolute_value(self) -> None:
        clock = FrozenClock(now=1000.0)
        clock.set(2000.0)
        assert clock.now() == 2000.0

    def test_set_forward_moves_monotonic_by_the_same_delta(self) -> None:
        clock = FrozenClock(now=1000.0, monotonic=50.0)
        clock.set(1010.0)
        assert clock.monotonic() == 60.0

    def test_set_backward_leaves_monotonic_time_unmoved(self) -> None:
        """Mirrors a real NTP correction: monotonic time never runs backwards."""
        clock = FrozenClock(now=1000.0, monotonic=50.0)
        clock.set(990.0)
        assert clock.now() == 990.0
        assert clock.monotonic() == 50.0

    def test_monotonic_never_decreases_across_a_backward_wall_clock_step(self) -> None:
        clock = FrozenClock(now=1000.0, monotonic=50.0)
        before = clock.monotonic()
        clock.set(500.0)  # a 500-second step backward in wall time
        assert clock.monotonic() >= before

    def test_repeated_backward_steps_still_never_move_monotonic_back(self) -> None:
        clock = FrozenClock(now=1000.0, monotonic=100.0)
        readings = [clock.monotonic()]
        for target in (900.0, 800.0, 950.0, 1200.0, 300.0):
            clock.set(target)
            readings.append(clock.monotonic())
        assert readings == sorted(readings)

    def test_default_monotonic_starts_at_zero(self) -> None:
        assert FrozenClock(now=12345.0).monotonic() == 0.0


class TestDriftMonitorConstruction:
    def test_valid_thresholds_are_accepted(self) -> None:
        monitor = DriftMonitor(warn_ms=250, critical_ms=1000)
        assert monitor.warn_ms == 250
        assert monitor.critical_ms == 1000

    @pytest.mark.parametrize(("warn", "critical"), [(0, 1000), (-1, 1000), (250, 0)])
    def test_non_positive_thresholds_are_rejected(self, warn: float, critical: float) -> None:
        with pytest.raises(ValueError, match="must be positive"):
            DriftMonitor(warn_ms=warn, critical_ms=critical)

    def test_inverted_thresholds_are_rejected(self) -> None:
        with pytest.raises(ValueError, match="below critical"):
            DriftMonitor(warn_ms=1000, critical_ms=250)

    def test_equal_thresholds_are_rejected(self) -> None:
        with pytest.raises(ValueError, match="below critical"):
            DriftMonitor(warn_ms=500, critical_ms=500)


class TestDriftClassificationIsOnAbsoluteMagnitude:
    """A local clock behind the venue is exactly as dangerous as one ahead."""

    def test_zero_offset_is_ok(self) -> None:
        monitor = DriftMonitor(warn_ms=250, critical_ms=1000)
        assert monitor.classify(0) == DriftStatus.OK

    def test_small_positive_offset_is_ok(self) -> None:
        monitor = DriftMonitor(warn_ms=250, critical_ms=1000)
        assert monitor.classify(100) == DriftStatus.OK

    def test_small_negative_offset_is_ok(self) -> None:
        monitor = DriftMonitor(warn_ms=250, critical_ms=1000)
        assert monitor.classify(-100) == DriftStatus.OK

    def test_a_negative_offset_past_critical_is_still_critical(self) -> None:
        """The failure mode a signed comparison would miss entirely.

        The config default in conftest.VALID_TRADING_VALUES sets critical at 900ms,
        which is where this specific number comes from.
        """
        monitor = DriftMonitor(warn_ms=250, critical_ms=900)
        assert monitor.classify(-900) == DriftStatus.CRITICAL

    def test_a_positive_offset_past_critical_is_critical(self) -> None:
        monitor = DriftMonitor(warn_ms=250, critical_ms=900)
        assert monitor.classify(900) == DriftStatus.CRITICAL

    def test_positive_and_negative_offsets_of_equal_magnitude_classify_identically(
        self,
    ) -> None:
        monitor = DriftMonitor(warn_ms=250, critical_ms=1000)
        for magnitude in (0, 100, 249, 250, 500, 900, 999, 1000, 5000):
            assert monitor.classify(magnitude) == monitor.classify(-magnitude)

    def test_warn_boundary_is_inclusive(self) -> None:
        monitor = DriftMonitor(warn_ms=250, critical_ms=1000)
        assert monitor.classify(250) == DriftStatus.WARN
        assert monitor.classify(-250) == DriftStatus.WARN
        assert monitor.classify(249.999) == DriftStatus.OK

    def test_critical_boundary_is_inclusive(self) -> None:
        monitor = DriftMonitor(warn_ms=250, critical_ms=1000)
        assert monitor.classify(1000) == DriftStatus.CRITICAL
        assert monitor.classify(-1000) == DriftStatus.CRITICAL
        assert monitor.classify(999.999) == DriftStatus.WARN

    def test_just_under_critical_is_warn_not_critical(self) -> None:
        monitor = DriftMonitor(warn_ms=250, critical_ms=900)
        assert monitor.classify(-900) == DriftStatus.CRITICAL
        assert monitor.classify(-899.999) == DriftStatus.WARN


class TestDriftMonitorObserve:
    def test_positive_offset_means_local_clock_is_ahead(self) -> None:
        monitor = DriftMonitor(warn_ms=250, critical_ms=1000)
        reading = monitor.observe(local_ts=1000.5, reference_ts=1000.0)
        assert reading.offset_ms == pytest.approx(500.0)

    def test_negative_offset_means_local_clock_is_behind(self) -> None:
        monitor = DriftMonitor(warn_ms=250, critical_ms=1000)
        reading = monitor.observe(local_ts=999.1, reference_ts=1000.0)
        assert reading.offset_ms == pytest.approx(-900.0)

    def test_observe_uses_the_same_classification_as_classify(self) -> None:
        """A half-second offset, which is exactly representable in binary.

        999.1 - 1000.0 is -899.9999999999something, so an offset chosen for
        readability would be testing float subtraction rather than agreement
        between the two code paths.
        """
        monitor = DriftMonitor(warn_ms=250, critical_ms=500)
        reading = monitor.observe(local_ts=999.5, reference_ts=1000.0)
        assert reading.offset_ms == -500.0
        assert reading.status == monitor.classify(-500.0)
        assert reading.status == DriftStatus.CRITICAL

    def test_is_critical_property_matches_status(self) -> None:
        monitor = DriftMonitor(warn_ms=250, critical_ms=1000)
        assert monitor.observe(local_ts=999.0, reference_ts=1000.0).is_critical is True
        assert monitor.observe(local_ts=1000.0, reference_ts=1000.0).is_critical is False

    def test_observe_updates_last(self) -> None:
        monitor = DriftMonitor(warn_ms=250, critical_ms=1000)
        monitor.observe(local_ts=1000.0, reference_ts=1000.0)
        monitor.observe(local_ts=1000.9, reference_ts=1000.0)
        assert monitor.last is not None
        assert monitor.last.offset_ms == pytest.approx(900.0)


class TestLastIsNoneBeforeFirstObservation:
    """"Never checked" and "checked and perfect" must not display identically."""

    def test_last_is_none_on_a_fresh_monitor(self) -> None:
        monitor = DriftMonitor(warn_ms=250, critical_ms=1000)
        assert monitor.last is None

    def test_last_becomes_non_none_after_one_observation(self) -> None:
        monitor = DriftMonitor(warn_ms=250, critical_ms=1000)
        monitor.observe(local_ts=1000.0, reference_ts=1000.0)
        assert monitor.last is not None

    def test_a_perfect_reading_is_still_distinguishable_from_unmeasured(self) -> None:
        monitor = DriftMonitor(warn_ms=250, critical_ms=1000)
        monitor.observe(local_ts=1000.0, reference_ts=1000.0)
        assert monitor.last is not None
        assert monitor.last.offset_ms == 0.0
        assert monitor.last.status == DriftStatus.OK
