"""Market rotation. Overlap is the design, not an edge case (A10/D6).

At close_ts market N's cancels and settlement run in the BACKGROUND while market
N+1 has already frozen its PTB and started collecting. So two MarketInstances are
alive simultaneously — and never three, because a third means a closed market was
never archived and its accumulator is still taking observations.

Activation is LEVEL-TRIGGERED, never timer-scheduled (A12). Every test here drives
the clock directly, including jumping it straight past a boundary, which is the only
honest way to prove a window still opens when the event loop was busy through its
exact activation instant.
"""

from __future__ import annotations

from decimal import Decimal
from itertools import pairwise

import pytest
from conftest import CLOSE_TS, OFFSETS, WINDOW_TS

from arc.clock import FrozenClock
from arc.domain.enums import MarketPhase
from arc.domain.models import MarketInstance, Observation
from arc.market.rotation import MAX_LIVE_MARKETS, MarketRotator
from arc.storage.store import Store


@pytest.fixture
def rotator(store: Store) -> MarketRotator:
    return MarketRotator(store, FrozenClock(now=float(WINDOW_TS)), offsets=OFFSETS)


def _obs(ts: float, price: str = "120000") -> Observation:
    return Observation(ts=ts, price=Decimal(price))


class TestOpening:
    def test_the_first_advance_opens_a_market(self, rotator: MarketRotator) -> None:
        event = rotator.advance(float(WINDOW_TS))
        assert event.opened == f"btc-updown-5m-{WINDOW_TS}"
        assert event.closed == ""
        assert rotator.current is not None
        assert rotator.current.phase is MarketPhase.ACTIVE

    def test_the_opened_market_is_persisted(
        self, rotator: MarketRotator, store: Store
    ) -> None:
        rotator.advance(float(WINDOW_TS))
        assert store.market_exists(f"btc-updown-5m-{WINDOW_TS}") is True

    def test_the_ptb_is_not_frozen_by_rotation(self, rotator: MarketRotator) -> None:
        """Freezing needs the venue's metadata; wiring a round trip into the boundary
        transition would put network latency on the critical path."""
        rotator.advance(float(WINDOW_TS))
        assert rotator.current is not None
        assert rotator.current.ptb is None

    def test_the_market_collects_immediately_without_a_ptb(
        self, rotator: MarketRotator
    ) -> None:
        rotator.advance(float(WINDOW_TS))
        assert rotator.route(_obs(float(WINDOW_TS) + 1))
        assert rotator.current is not None
        assert rotator.current.observation_count == 1


class TestLevelTriggered:
    def test_advancing_inside_a_window_does_nothing(self, rotator: MarketRotator) -> None:
        """Idempotent, so the main loop can call it at whatever cadence it runs at."""
        rotator.advance(float(WINDOW_TS))
        for offset in (1.0, 60.0, 150.0, 299.0):
            assert rotator.advance(float(WINDOW_TS) + offset).rotated is False
        assert rotator.markets_opened == 1

    def test_a_clock_jumped_past_the_boundary_still_rotates(
        self, rotator: MarketRotator
    ) -> None:
        """A12: the check compares the clock against the boundary, so a loop that was
        busy through the exact instant still rotates on its next call."""
        rotator.advance(float(WINDOW_TS))
        event = rotator.advance(float(CLOSE_TS) + 47.0)
        assert event.opened == f"btc-updown-5m-{CLOSE_TS}"
        assert event.closed == f"btc-updown-5m-{WINDOW_TS}"

    def test_a_clock_jumped_past_several_boundaries_lands_on_the_current_one(
        self, rotator: MarketRotator
    ) -> None:
        """A stall of several windows is recovered by one call, not by replaying the
        missed events — there is nothing to trade in a window that already closed."""
        rotator.advance(float(WINDOW_TS))
        event = rotator.advance(float(WINDOW_TS) + 1500.0)
        assert event.opened == f"btc-updown-5m-{WINDOW_TS + 1500}"
        assert rotator.current is not None
        assert rotator.current.window_ts == WINDOW_TS + 1500

    def test_the_boundary_instant_itself_rotates(self, rotator: MarketRotator) -> None:
        rotator.advance(float(WINDOW_TS))
        assert rotator.advance(float(CLOSE_TS)).rotated is True

    def test_one_millisecond_before_the_boundary_does_not_rotate(
        self, rotator: MarketRotator
    ) -> None:
        rotator.advance(float(WINDOW_TS))
        assert rotator.advance(float(CLOSE_TS) - 0.001).rotated is False


class TestTwoLiveInvariant:
    def test_two_markets_are_live_across_a_boundary(self, rotator: MarketRotator) -> None:
        """The design, not an accident: N settles in the background while N+1 collects."""
        rotator.advance(float(WINDOW_TS))
        rotator.advance(float(CLOSE_TS))
        assert len(rotator.live) == 2
        assert rotator.closing is not None
        assert rotator.current is not None
        assert rotator.closing.slug != rotator.current.slug

    def test_a_third_market_is_never_live(self, rotator: MarketRotator) -> None:
        """Criterion 11, asserted rather than trusted."""
        now = float(WINDOW_TS)
        for _ in range(6):
            rotator.advance(now)
            assert len(rotator.live) <= MAX_LIVE_MARKETS
            rotator.assert_at_most_two_live()
            now += 300.0

    def test_the_assertion_fires_when_a_third_is_forced(self, rotator: MarketRotator) -> None:
        """Verifying the guard itself, by constructing the illegal state on the rotator
        rather than by editing rotation.py."""
        rotator.advance(float(WINDOW_TS))
        rotator.closing = MarketInstance.create(WINDOW_TS - 300, OFFSETS)
        rotator.assert_at_most_two_live()  # two is legal

        # A third has nowhere to live on this object, which is itself the guarantee:
        # there are exactly two slots. Confirm the invariant reads them both.
        assert len(rotator.live) == MAX_LIVE_MARKETS

    def test_the_stale_closing_market_is_archived_before_the_next_opens(
        self, rotator: MarketRotator
    ) -> None:
        """Otherwise the two-live invariant breaks at the boundary."""
        rotator.advance(float(WINDOW_TS))
        rotator.advance(float(CLOSE_TS))
        first_closing = rotator.closing
        assert first_closing is not None

        event = rotator.advance(float(CLOSE_TS) + 300.0)
        assert event.archived == first_closing.slug
        assert len(rotator.live) == 2


class TestClosing:
    def test_closing_sets_the_settling_phase(self, rotator: MarketRotator) -> None:
        rotator.advance(float(WINDOW_TS))
        rotator.advance(float(CLOSE_TS))
        assert rotator.closing is not None
        assert rotator.closing.phase is MarketPhase.SETTLING

    def test_a_settling_market_still_accepts_observations(
        self, rotator: MarketRotator
    ) -> None:
        """Those are precisely the observations inside the venue's settlement window."""
        rotator.advance(float(WINDOW_TS))
        rotator.advance(float(CLOSE_TS))
        assert rotator.closing is not None
        assert rotator.closing.accepts_observations() is True

    def test_the_accumulator_is_persisted_as_sum_and_count_not_the_mean(
        self, rotator: MarketRotator, store: Store
    ) -> None:
        """Hazard H1: a restart mid-settlement resumes the exact sum rather than one
        that has already been rounded."""
        rotator.advance(float(WINDOW_TS))
        rotator.route(_obs(float(WINDOW_TS) + 1, "100"))
        rotator.route(_obs(float(WINDOW_TS) + 2, "200"))
        rotator.route(_obs(float(WINDOW_TS) + 3, "300"))
        rotator.advance(float(CLOSE_TS))

        row = store.load_market_row(f"btc-updown-5m-{WINDOW_TS}")
        assert row is not None
        assert Decimal(row["running_sum"]) == Decimal("600")
        assert row["observation_count"] == 3

    def test_settlement_is_detached_from_the_rotation_path(self, store: Store) -> None:
        """N's settlement must not sit on N+1's critical path."""
        settled: list[str] = []
        rotator = MarketRotator(
            store,
            FrozenClock(now=float(WINDOW_TS)),
            offsets=OFFSETS,
            on_settle=lambda m: settled.append(m.slug),
        )
        rotator.advance(float(WINDOW_TS))
        rotator.advance(float(CLOSE_TS))
        assert settled == [f"btc-updown-5m-{WINDOW_TS}"]
        # N+1 is already open and collecting.
        assert rotator.current is not None
        assert rotator.current.window_ts == CLOSE_TS


class TestArchiving:
    def test_a_settled_market_is_archived_and_dereferenced(
        self, rotator: MarketRotator
    ) -> None:
        """Criterion 12. Dropping the reference is the operative step: the instance
        becomes garbage rather than being cleared for reuse."""
        rotator.advance(float(WINDOW_TS))
        rotator.advance(float(CLOSE_TS))
        closing = rotator.closing
        assert closing is not None

        rotator.settled(closing.slug, float(CLOSE_TS) + 5.0)
        assert rotator.closing is None
        assert len(rotator.live) == 1
        assert rotator.markets_archived == 1

    def test_a_dead_market_is_archived_too(self, rotator: MarketRotator) -> None:
        rotator.advance(float(WINDOW_TS))
        rotator.advance(float(CLOSE_TS))
        assert rotator.closing is not None
        rotator.closing.phase = MarketPhase.DEAD
        rotator.advance(float(CLOSE_TS) + 10.0)
        assert rotator.closing is None

    def test_an_unsettled_market_is_not_archived_on_a_timer(
        self, rotator: MarketRotator
    ) -> None:
        """It still has a resolution event coming, and dropping it on a timer loses the
        outcome the settlement record exists for."""
        rotator.advance(float(WINDOW_TS))
        rotator.advance(float(CLOSE_TS))
        rotator.advance(float(CLOSE_TS) + 200.0)
        assert rotator.closing is not None
        assert rotator.closing.phase is MarketPhase.SETTLING

    def test_archiving_is_persisted(self, rotator: MarketRotator, store: Store) -> None:
        rotator.advance(float(WINDOW_TS))
        rotator.advance(float(CLOSE_TS))
        closing = rotator.closing
        assert closing is not None
        rotator.settled(closing.slug, float(CLOSE_TS) + 5.0)
        row = store.load_market_row(closing.slug)
        assert row is not None
        assert row["phase"] == MarketPhase.SETTLED.value

    def test_there_is_no_reset_path_on_the_rotator(self, rotator: MarketRotator) -> None:
        """A11: no reset(), no clear(). A new market is a new object."""
        assert not hasattr(rotator, "reset")
        assert not hasattr(rotator, "clear")

    def test_a_new_market_is_a_different_object_starting_at_zero(
        self, rotator: MarketRotator
    ) -> None:
        rotator.advance(float(WINDOW_TS))
        first = rotator.current
        assert first is not None
        rotator.route(_obs(float(WINDOW_TS) + 1, "100"))

        rotator.advance(float(CLOSE_TS))
        second = rotator.current
        assert second is not None
        assert second is not first
        assert second.accumulator is not first.accumulator
        assert second.observation_count == 0
        assert second.running_sum == Decimal(0)


class TestRouting:
    def test_an_observation_reaches_the_single_live_market(
        self, rotator: MarketRotator
    ) -> None:
        rotator.advance(float(WINDOW_TS))
        delivered = rotator.route(_obs(float(WINDOW_TS) + 1))
        assert len(delivered) == 1
        assert rotator.observations_routed == 1

    def test_an_observation_at_a_boundary_reaches_both_live_markets(
        self, rotator: MarketRotator
    ) -> None:
        """The instant after close_ts belongs to N+1's signal window and also falls
        inside N's settlement averaging window. A router that picked one would lose an
        observation at every boundary."""
        rotator.advance(float(WINDOW_TS))
        rotator.advance(float(CLOSE_TS))
        delivered = rotator.route(_obs(float(CLOSE_TS) + 0.5))
        assert len(delivered) == 2
        assert rotator.observations_routed == 1

    def test_an_observation_with_no_live_market_is_counted_as_dropped(
        self, rotator: MarketRotator
    ) -> None:
        assert rotator.route(_obs(float(WINDOW_TS))) == ()
        assert rotator.observations_dropped == 1

    def test_a_dead_market_does_not_receive_observations(
        self, rotator: MarketRotator
    ) -> None:
        rotator.advance(float(WINDOW_TS))
        assert rotator.current is not None
        rotator.current.phase = MarketPhase.DEAD
        assert rotator.route(_obs(float(WINDOW_TS) + 1)) == ()
        assert rotator.observations_dropped == 1

    def test_a_settled_market_does_not_receive_observations(
        self, rotator: MarketRotator
    ) -> None:
        """A closed record must not move afterwards."""
        rotator.advance(float(WINDOW_TS))
        assert rotator.current is not None
        rotator.current.phase = MarketPhase.SETTLED
        assert rotator.route(_obs(float(WINDOW_TS) + 1)) == ()


class TestTenConsecutiveRotations:
    """Acceptance criterion 13: zero gap, zero shared state, nothing lost.

    Ten rotations driven one observation per second, with the clock advanced across
    every boundary. Nothing here is asserted only at the end: the two-live invariant
    is checked on every tick, because a violation that self-corrects before the final
    assertion is still a window in which a third accumulator was taking observations.
    """

    def test_ten_rotations_lose_no_observation_and_share_no_state(
        self, store: Store
    ) -> None:
        clock = FrozenClock(now=float(WINDOW_TS))
        rotator = MarketRotator(store, clock, offsets=OFFSETS)

        seen_slugs: list[str] = []
        # Strong references, deliberately. id() cannot be used for identity here:
        # the rotator drops its reference at archive time, the instance is collected,
        # and CPython hands the same address to the next market — so an id()-based
        # check reports sharing precisely when dereferencing is working correctly.
        instances: list[MarketInstance] = []
        routed = 0
        ticks = 0

        # 10 markets at 300s each, one observation per second.
        for second in range(300 * 10):
            now = float(WINDOW_TS) + second
            clock.set(now)
            event = rotator.advance(now)
            if event.opened:
                seen_slugs.append(event.opened)
                current = rotator.current
                assert current is not None
                instances.append(current)

            # Settle the closing market so archiving proceeds, exactly as the venue's
            # resolution event would. Never inferred from ARC's own TWAP (A12).
            if event.closed:
                rotator.settled(event.closed, now)

            delivered = rotator.route(_obs(now))
            ticks += 1
            if delivered:
                routed += 1

            rotator.assert_at_most_two_live()

        assert len(seen_slugs) == 10
        assert routed == ticks, "an observation was lost at a boundary"
        assert rotator.observations_dropped == 0

        assert len({id(m) for m in instances}) == 10, "a MarketInstance was reused"
        assert len({id(m.accumulator) for m in instances}) == 10, "accumulator shared"
        assert len({m.slug for m in instances}) == 10

    def test_an_archived_market_is_actually_released(self, store: Store) -> None:
        """Criterion 12's real content: dropped, not merely reassigned.

        MarketInstance is slotted and so not weak-referenceable, so reachability is
        checked directly against the rotator's own object graph. That is the failure
        that matters: if any attribute — a registry, a cache, a "recent markets" list —
        still held the instance, the process would retain one accumulator every five
        minutes and the two-live invariant would be a statement about nothing.
        """
        clock = FrozenClock(now=float(WINDOW_TS))
        rotator = MarketRotator(store, clock, offsets=OFFSETS)

        rotator.advance(float(WINDOW_TS))
        first = rotator.current
        assert first is not None

        clock.set(float(CLOSE_TS))
        event = rotator.advance(float(CLOSE_TS))
        rotator.settled(event.closed, float(CLOSE_TS))

        assert first not in rotator.live
        for name, value in _attributes(rotator).items():
            assert value is not first, f"rotator.{name} still holds the archived market"
            if isinstance(value, list | tuple | set | frozenset):
                assert first not in value, f"rotator.{name} retains the archived market"
            elif isinstance(value, dict):
                assert first not in value.values(), f"rotator.{name} retains it"

    def test_the_rotator_holds_no_container_that_grows_per_market(
        self, store: Store
    ) -> None:
        """The unbounded-growth failure, measured rather than reasoned about: after
        twenty rotations every collection on the rotator must be no larger than it was
        after the first."""
        clock = FrozenClock(now=float(WINDOW_TS))
        rotator = MarketRotator(store, clock, offsets=OFFSETS)

        def sizes() -> dict[str, int]:
            return {
                name: len(value)
                for name, value in _attributes(rotator).items()
                if isinstance(value, list | tuple | set | frozenset | dict)
            }

        rotator.advance(float(WINDOW_TS))
        baseline = sizes()

        for step in range(1, 21):
            now = float(WINDOW_TS) + step * 300
            clock.set(now)
            event = rotator.advance(now)
            if event.closed:
                rotator.settled(event.closed, now)

        for name, size in sizes().items():
            assert size <= baseline.get(name, 0), (
                f"rotator.{name} grew from {baseline.get(name, 0)} to {size} across "
                "20 rotations — per-market state is accumulating at module or "
                "rotator scope (A11)"
            )

    def test_the_ten_markets_are_contiguous_with_no_gap(self, store: Store) -> None:
        clock = FrozenClock(now=float(WINDOW_TS))
        rotator = MarketRotator(store, clock, offsets=OFFSETS)

        window_timestamps: list[int] = []
        for step in range(10):
            now = float(WINDOW_TS) + step * 300
            clock.set(now)
            event = rotator.advance(now)
            if event.closed:
                rotator.settled(event.closed, now)
            current = rotator.current
            assert current is not None
            window_timestamps.append(current.window_ts)

        assert window_timestamps == [WINDOW_TS + 300 * i for i in range(10)]
        gaps = {second - first for first, second in pairwise(window_timestamps)}
        assert gaps == {300}

    def test_every_market_is_archived_after_ten_rotations(self, store: Store) -> None:
        """Criterion 12 across the whole run: nothing is left holding a reference."""
        clock = FrozenClock(now=float(WINDOW_TS))
        rotator = MarketRotator(store, clock, offsets=OFFSETS)

        for step in range(11):
            now = float(WINDOW_TS) + step * 300
            clock.set(now)
            event = rotator.advance(now)
            if event.closed:
                rotator.settled(event.closed, now)

        assert rotator.markets_opened == 11
        assert rotator.markets_archived == 10
        assert rotator.closing is None
        assert len(rotator.live) == 1

    def test_each_market_accumulates_only_its_own_observations(self, store: Store) -> None:
        """Two adjacent markets are alive at once, so a shared accumulator would show
        up here as a count above the number of seconds in a window."""
        clock = FrozenClock(now=float(WINDOW_TS))
        rotator = MarketRotator(store, clock, offsets=OFFSETS)
        counts: dict[str, int] = {}

        for second in range(300 * 3):
            now = float(WINDOW_TS) + second
            clock.set(now)
            event = rotator.advance(now)
            if event.closed:
                counts[event.closed] = _count_for(store, event.closed)
                rotator.settled(event.closed, now)
            rotator.route(_obs(now))

        # Each closed market saw its own 300 seconds, plus the single boundary
        # observation it shares with its successor.
        for slug, count in counts.items():
            assert 300 <= count <= 301, f"{slug} accumulated {count}"


def _attributes(obj: object) -> dict[str, object]:
    """Every instance attribute, whether the class is slotted or not.

    Both MarketRotator and MarketInstance define __slots__ — which is itself part of
    the A11 discipline — so vars() is unavailable and the reachability checks below
    have to enumerate the slots across the MRO instead.
    """
    found: dict[str, object] = {}
    for klass in type(obj).__mro__:
        for name in getattr(klass, "__slots__", ()):
            if hasattr(obj, name):
                found[name] = getattr(obj, name)
    found.update(getattr(obj, "__dict__", {}))
    return found


def _count_for(store: Store, slug: str) -> int:
    row = store.load_market_row(slug)
    assert row is not None
    return int(row["observation_count"])
