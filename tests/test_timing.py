"""Market-grid timing: the grid, the slug, level-triggered activation, countdown.

Everything in arc.domain.timing is a pure function of a timestamp — nothing reads
a clock. That is what makes these tests possible at all, and it is why they can
assert the awkward cases directly: the loop that was blocked straight through an
activation instant, the market that closed but has not settled, the moment exactly
on a grid boundary.

The grid being contiguous (next window_ts IS this close_ts) is asserted repeatedly
below rather than once. It is the property that decides how many market instances
are alive at a boundary — two, never three (A10/D6) — and an off-by-one there
would produce a gap in which no market exists, or an overlap in which three do.
"""

from __future__ import annotations

from decimal import Decimal
from itertools import pairwise

import pytest
from conftest import CLOSE_TS, OFFSETS, WINDOW_TS

from arc.domain.timing import (
    MARKET_DURATION_SECONDS,
    SETTLEMENT_WINDOW_SECONDS,
    SLUG_PREFIX,
    activation_ts,
    cancel_ts,
    close_ts_for,
    format_countdown,
    is_window_open,
    next_window_ts,
    settlement_determined_fraction,
    settlement_window_start,
    slug_for,
    window_ts_for,
    windows_by_priority,
)


class TestGridAlignment:
    def test_market_duration_is_five_minutes(self) -> None:
        assert MARKET_DURATION_SECONDS == 300

    def test_exact_boundary_belongs_to_the_market_it_starts(self) -> None:
        """At t == window_ts the new market has begun, not the old one continuing."""
        assert window_ts_for(float(WINDOW_TS)) == WINDOW_TS
        assert window_ts_for(float(CLOSE_TS)) == CLOSE_TS

    @pytest.mark.parametrize("elapsed", [0.0, 0.001, 1.0, 150.0, 299.0, 299.999])
    def test_every_instant_inside_a_market_maps_to_its_start(self, elapsed: float) -> None:
        assert window_ts_for(WINDOW_TS + elapsed) == WINDOW_TS

    def test_the_instant_before_close_is_still_this_market(self) -> None:
        assert window_ts_for(CLOSE_TS - 0.000001) == WINDOW_TS

    def test_result_is_always_grid_aligned(self) -> None:
        for offset in range(0, 900, 7):
            assert window_ts_for(WINDOW_TS + offset + 0.5) % 300 == 0

    def test_alignment_holds_for_unaligned_input_timestamps(self) -> None:
        """A real clock never lands on a multiple of 300."""
        assert window_ts_for(1754400137.482) == 1754400000

    def test_floor_division_not_truncation_toward_zero(self) -> None:
        """Pre-epoch timestamps are absurd here, but int() would floor the wrong way."""
        assert window_ts_for(-1.0) == -300


class TestGridIsContiguous:
    """The next market's window_ts IS this market's close_ts (A5).

    Not "close_ts plus one second". A gap would leave an instant belonging to no
    market; an overlap would put three instances alive at a boundary instead of two.
    """

    def test_next_window_equals_close(self) -> None:
        assert next_window_ts(WINDOW_TS) == close_ts_for(WINDOW_TS)

    def test_close_is_exactly_one_duration_later(self) -> None:
        assert close_ts_for(WINDOW_TS) == WINDOW_TS + MARKET_DURATION_SECONDS

    def test_no_instant_falls_between_two_markets(self) -> None:
        for step in range(12):
            start = WINDOW_TS + step * 300
            close = close_ts_for(start)
            # The instant of close belongs to the next market, with nothing between.
            assert window_ts_for(float(close)) == next_window_ts(start)

    def test_walking_the_grid_never_skips_or_repeats(self) -> None:
        seen = [WINDOW_TS]
        for _ in range(50):
            seen.append(next_window_ts(seen[-1]))
        assert len(set(seen)) == len(seen)
        assert all(b - a == 300 for a, b in pairwise(seen))


class TestSlug:
    def test_slug_format(self) -> None:
        assert slug_for(WINDOW_TS) == f"btc-updown-5m-{WINDOW_TS}"

    def test_prefix_is_used(self) -> None:
        assert slug_for(WINDOW_TS).startswith(SLUG_PREFIX)

    def test_slug_carries_no_separator_or_padding(self) -> None:
        """The slug is matched against the venue's, so extra characters break lookup."""
        assert slug_for(WINDOW_TS) == SLUG_PREFIX + str(WINDOW_TS)

    def test_distinct_windows_get_distinct_slugs(self) -> None:
        slugs = {slug_for(WINDOW_TS + i * 300) for i in range(20)}
        assert len(slugs) == 20

    def test_slug_is_recoverable_back_to_the_timestamp(self) -> None:
        """Restart recovery reads the slug from storage and needs the grid time back."""
        assert int(slug_for(WINDOW_TS).removeprefix(SLUG_PREFIX)) == WINDOW_TS


class TestActivationInstant:
    @pytest.mark.parametrize("offset", OFFSETS)
    def test_activation_is_offset_seconds_before_close(self, offset: int) -> None:
        assert activation_ts(CLOSE_TS, offset) == CLOSE_TS - offset

    def test_larger_offsets_activate_earlier(self) -> None:
        instants = [activation_ts(CLOSE_TS, o) for o in sorted(OFFSETS, reverse=True)]
        assert instants == sorted(instants)


class TestLevelTriggeredActivation:
    """is_window_open re-answers "is the level satisfied at t" (A12).

    The distinction from a timer is the whole point. A scheduled timer that fires
    while the event loop is busy is simply missed — the window is lost and nothing
    anywhere records that it should have happened. A level check that a blocked
    loop reaches late still sees the window open and acts late instead of never.
    """

    @pytest.mark.parametrize("offset", OFFSETS)
    def test_closed_before_activation(self, offset: int) -> None:
        assert not is_window_open(CLOSE_TS - offset - 0.001, CLOSE_TS, offset)

    @pytest.mark.parametrize("offset", OFFSETS)
    def test_open_exactly_at_activation(self, offset: int) -> None:
        """The boundary is inclusive: activation_ts <= now."""
        assert is_window_open(float(CLOSE_TS - offset), CLOSE_TS, offset)

    @pytest.mark.parametrize("offset", OFFSETS)
    def test_open_throughout_the_window(self, offset: int) -> None:
        assert is_window_open(CLOSE_TS - offset + 0.001, CLOSE_TS, offset)
        assert is_window_open(CLOSE_TS - 0.001, CLOSE_TS, offset)

    @pytest.mark.parametrize("offset", OFFSETS)
    def test_closed_at_and_after_close(self, offset: int) -> None:
        """The upper bound is exclusive: at close the market is no longer tradable."""
        assert not is_window_open(float(CLOSE_TS), CLOSE_TS, offset)
        assert not is_window_open(CLOSE_TS + 0.001, CLOSE_TS, offset)

    def test_a_loop_that_jumped_past_activation_still_sees_the_window_open(self) -> None:
        """The case a timer loses outright.

        The event loop was busy from before activation until well after it, so no
        pass ever observed t == activation_ts. The window must still be open.
        """
        offset = 3
        before = CLOSE_TS - offset - 0.5
        after = CLOSE_TS - offset + 1.2  # activation instant never observed
        assert not is_window_open(before, CLOSE_TS, offset)
        assert is_window_open(after, CLOSE_TS, offset)

    def test_a_loop_that_jumped_past_the_entire_window_sees_it_closed(self) -> None:
        """Late is recoverable; past close is not, and must not be treated as open."""
        assert not is_window_open(CLOSE_TS + 0.5, CLOSE_TS, 3)

    def test_repeated_calls_at_the_same_instant_agree(self) -> None:
        """Level-triggered means idempotent: no internal edge state to consume."""
        now = float(CLOSE_TS - 5)
        assert len({is_window_open(now, CLOSE_TS, 5) for _ in range(5)}) == 1

    def test_all_earlier_windows_are_open_once_the_latest_is(self) -> None:
        """At t=close-3 every configured window has activated (A12)."""
        now = float(CLOSE_TS - 3)
        assert all(is_window_open(now, CLOSE_TS, o) for o in OFFSETS)

    def test_only_the_earliest_window_is_open_at_its_own_activation(self) -> None:
        now = float(CLOSE_TS - 15)
        open_windows = {o for o in OFFSETS if is_window_open(now, CLOSE_TS, o)}
        assert open_windows == {15}


class TestCancellationSweepIsAPhaseGateNotATimingGate:
    """Crossing cancel_ts moves the market to CANCELLING (A10/D1).

    The Risk Engine's existing phase check then denies new submissions. No code
    compares a clock to decide whether an individual order is "too late".
    """

    def test_cancel_instant_is_lead_ms_before_close(self) -> None:
        assert cancel_ts(CLOSE_TS, 500) == CLOSE_TS - 0.5
        assert cancel_ts(CLOSE_TS, 2999) == CLOSE_TS - 2.999

    def test_a_larger_lead_starts_the_sweep_earlier(self) -> None:
        assert cancel_ts(CLOSE_TS, 1000) < cancel_ts(CLOSE_TS, 500)

    def test_the_earliest_window_opens_before_the_sweep_begins(self) -> None:
        """Config invariant 10 guarantees this; here it is as an arithmetic fact."""
        earliest = min(OFFSETS)
        assert activation_ts(CLOSE_TS, earliest) < cancel_ts(CLOSE_TS, 500)

    def test_a_lead_longer_than_the_earliest_window_inverts_the_order(self) -> None:
        """Exactly the configuration invariant 10 refuses, shown here as timing."""
        assert activation_ts(CLOSE_TS, 3) > cancel_ts(CLOSE_TS, 4000)


class TestCountdownMatchesTheVenuePage:
    """Floored, clamped, never negative.

    Floored because with 299.4s left the venue shows 04:59; a countdown that
    rounded would show 05:00 and disagree with the screen the operator is
    comparing it against, during the seconds that matter most.
    """

    @pytest.mark.parametrize(
        ("remaining", "expected"),
        [
            (300.0, "05:00"),
            (299.999, "04:59"),
            (299.4, "04:59"),
            (60.0, "01:00"),
            (59.9, "00:59"),
            (15.0, "00:15"),
            (3.9, "00:03"),
            (1.0, "00:01"),
            (0.999, "00:00"),
        ],
    )
    def test_floors_never_rounds(self, remaining: float, expected: str) -> None:
        assert format_countdown(CLOSE_TS - remaining, CLOSE_TS) == expected

    def test_clamped_at_zero_at_close(self) -> None:
        assert format_countdown(float(CLOSE_TS), CLOSE_TS) == "00:00"

    @pytest.mark.parametrize("overshoot", [0.001, 1.0, 60.0, 3600.0])
    def test_never_renders_a_negative_timer(self, overshoot: float) -> None:
        """A market that has closed but not yet settled sits in exactly this state."""
        rendered = format_countdown(CLOSE_TS + overshoot, CLOSE_TS)
        assert rendered == "00:00"
        assert "-" not in rendered

    def test_always_two_digit_zero_padded(self) -> None:
        for remaining in range(1, 301):
            rendered = format_countdown(CLOSE_TS - remaining, CLOSE_TS)
            minutes, _, seconds = rendered.partition(":")
            assert len(minutes) == 2 and len(seconds) == 2, rendered

    def test_seconds_never_reach_sixty(self) -> None:
        for tenths in range(0, 3001):
            rendered = format_countdown(CLOSE_TS - tenths / 10, CLOSE_TS)
            assert int(rendered.partition(":")[2]) < 60, rendered

    def test_countdown_is_monotonically_non_increasing(self) -> None:
        """A countdown that ever ticks up reads as a stalled or reset market."""
        previous = "99:99"
        for tenths in range(3000, -1, -1):
            rendered = format_countdown(CLOSE_TS - tenths / 10, CLOSE_TS)
            assert rendered <= previous, f"{rendered} came after {previous}"
            previous = rendered


class TestSettlementWindowIsObservationalOnly:
    """TRAP 1 (A5): 30s is a LOOKBACK LENGTH, not a publication rate.

    It says the venue averages the last 30 seconds of observations. It says nothing
    about how often the feed emits, and the two must never be inferred from each
    other or used to health-check each other.
    """

    def test_lookback_length_is_thirty_seconds(self) -> None:
        assert SETTLEMENT_WINDOW_SECONDS == 30

    def test_window_sits_before_close_not_straddling_it(self) -> None:
        """Placement is UNDOCUMENTED (A8/U1), which is why nothing decides on it."""
        assert settlement_window_start(CLOSE_TS) == CLOSE_TS - 30

    def test_window_length_is_overridable_for_the_open_question(self) -> None:
        """The assumption is recorded, not hardcoded, so real data can revise it."""
        assert settlement_window_start(CLOSE_TS, 60) == CLOSE_TS - 60

    def test_the_lookback_is_shorter_than_the_market(self) -> None:
        assert SETTLEMENT_WINDOW_SECONDS < MARKET_DURATION_SECONDS


class TestDeterminedFraction:
    """A7: (w - t) / w, clamped to [0, 1]. Display only — no decision reads it.

    This is why the later windows are the better-informed ones rather than the
    reckless ones: at t=3 ninety percent of the settlement average is already
    arithmetically fixed, so what remains is a liquidity question, not an
    information one.
    """

    @pytest.mark.parametrize(
        ("seconds_before_close", "expected"),
        [
            (30, "0"),
            (15, "0.5"),
            (10, "0.6666666666666666666666666667"),
            (7, "0.7666666666666666666666666667"),
            (5, "0.8333333333333333333333333333"),
            (3, "0.9"),
            (0, "1"),
        ],
    )
    def test_the_documented_table(self, seconds_before_close: int, expected: str) -> None:
        assert settlement_determined_fraction(seconds_before_close) == Decimal(expected)

    def test_half_is_exact_at_the_midpoint(self) -> None:
        assert settlement_determined_fraction(15) == Decimal("0.5")

    def test_three_second_window_is_ninety_percent_determined(self) -> None:
        """The claim the window ordering rests on."""
        assert settlement_determined_fraction(3) == Decimal("0.9")

    def test_clamped_to_zero_before_the_window_opens(self) -> None:
        for t in (30, 31, 100, 300):
            assert settlement_determined_fraction(t) == Decimal("0")

    def test_clamped_to_one_at_and_after_close(self) -> None:
        for t in (0, -0.5, -30):
            assert settlement_determined_fraction(t) == Decimal("1")

    def test_never_leaves_the_unit_interval(self) -> None:
        for tenths in range(-400, 4000):
            fraction = settlement_determined_fraction(tenths / 10)
            assert Decimal("0") <= fraction <= Decimal("1")

    def test_increases_as_close_approaches(self) -> None:
        fractions = [settlement_determined_fraction(t) for t in range(30, -1, -1)]
        assert fractions == sorted(fractions)
        assert fractions[0] == Decimal("0") and fractions[-1] == Decimal("1")

    def test_returns_decimal_never_float(self) -> None:
        """Consistent with the rest of the codebase even though nothing decides on it."""
        assert isinstance(settlement_determined_fraction(7), Decimal)

    def test_a_float_argument_is_converted_via_its_text_form(self) -> None:
        """str() first, so 0.1 does not enter as its binary approximation."""
        assert settlement_determined_fraction(15.0) == Decimal("0.5")

    @pytest.mark.parametrize("window", [0, -1, -30])
    def test_non_positive_window_length_is_refused(self, window: int) -> None:
        with pytest.raises(ValueError, match="must be positive"):
            settlement_determined_fraction(15, window)

    def test_a_custom_window_rescales_the_fraction(self) -> None:
        assert settlement_determined_fraction(30, 60) == Decimal("0.5")


class TestWindowPriority:
    """Ascending offset: the window closest to close goes first (A12)."""

    def test_documented_order(self) -> None:
        assert windows_by_priority(OFFSETS) == (3, 5, 7, 10, 15)

    def test_input_order_is_irrelevant(self) -> None:
        assert windows_by_priority((7, 15, 3, 10, 5)) == (3, 5, 7, 10, 15)

    def test_the_first_window_has_the_largest_determined_fraction(self) -> None:
        """Priority order and information order must agree, or the ranking is wrong."""
        ordered = windows_by_priority(OFFSETS)
        fractions = [settlement_determined_fraction(o) for o in ordered]
        assert fractions == sorted(fractions, reverse=True)

    def test_empty_and_single_are_handled(self) -> None:
        assert windows_by_priority(()) == ()
        assert windows_by_priority((3,)) == (3,)

    def test_returns_a_tuple_not_a_list(self) -> None:
        """Immutable, so a caller cannot reorder the shared priority sequence."""
        assert isinstance(windows_by_priority(OFFSETS), tuple)
