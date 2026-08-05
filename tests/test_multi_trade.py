"""Multi-trade mode: each fired window is independent, one intent each.

Multi-trade mode is `max_trades_per_market > 1`. Every fired window is decided on
its own merits, and each may produce exactly one intent — up to the quota, which is
the thing that stops "independent" from meaning "unbounded".
"""

from __future__ import annotations

from decimal import Decimal

from decision_fixtures import BASE_PTB, fill_window, fired_market, make_engine, trading

from arc.decision.engine import DecisionEngine
from arc.decision.reasons import SkipReason
from arc.domain.enums import DenialReason, Direction, MarketPhase
from arc.domain.models import MarketInstance
from arc.storage.store import Store

NOW = 1754400001.0


def _engine(store: Store, *, limit: int = 5) -> DecisionEngine:
    return make_engine(store, config=trading(max_trades_per_market=str(limit)))


def _market(store: Store, *, fired: tuple[int, ...], window_ts: int = 1754400000) -> MarketInstance:
    instance = fired_market(fired=fired, window_ts=window_ts)
    store.create_market(instance, NOW)
    return instance


class TestEachWindowIsIndependent:
    def test_three_fired_windows_produce_three_intents(self, store: Store) -> None:
        market = _market(store, fired=(7, 5, 3))
        outcome = _engine(store).decide(market, NOW)
        assert [i.offset_seconds for i in outcome.intents] == [3, 5, 7]

    def test_all_five_windows_can_trade(self, store: Store) -> None:
        market = _market(store, fired=(15, 10, 7, 5, 3))
        outcome = _engine(store).decide(market, NOW)
        assert len(outcome.intents) == 5
        assert len(store.intents_for(market.slug)) == 5

    def test_no_window_is_skipped_as_lower_priority(self, store: Store) -> None:
        """Priority ordering exists only to arbitrate a single budget. Applying it in
        multi-trade mode would silently make the mode single-trade."""
        market = _market(store, fired=(15, 10, 7, 5, 3))
        outcome = _engine(store).decide(market, NOW)
        assert all(d.skip is not SkipReason.LOWER_PRIORITY for d in outcome.decisions)

    def test_each_window_carries_its_own_frozen_trigger(self, store: Store) -> None:
        """Each window froze against the same PTB but its own buffer, so the triggers
        differ. One shared trigger across windows would collapse five configured
        thresholds into one."""
        market = _market(store, fired=(15, 10, 7, 5, 3))
        outcome = _engine(store).decide(market, NOW)
        by_offset = {i.offset_seconds: i for i in outcome.intents}
        for offset, intent in by_offset.items():
            assert intent.locked_trigger == market.window(offset).locked_trigger
            assert intent.buffer == market.window(offset).buffer
        triggers = {i.locked_trigger for i in outcome.intents}
        assert len(triggers) > 1

    def test_every_window_shares_the_one_frozen_ptb(self, store: Store) -> None:
        """A12: the PTB is fetched once at window_ts and is the same value for all
        five windows. A per-window fetch would give later windows a different
        reference and a different direction."""
        market = _market(store, fired=(15, 10, 7, 5, 3))
        outcome = _engine(store).decide(market, NOW)
        assert {i.ptb for i in outcome.intents} == {market.ptb}

    def test_each_intent_has_its_own_id(self, store: Store) -> None:
        market = _market(store, fired=(15, 10, 7, 5, 3))
        outcome = _engine(store).decide(market, NOW)
        ids = [i.intent_id for i in outcome.intents]
        assert len(set(ids)) == 5
        assert ids == [f"{market.slug}:{o}" for o in (3, 5, 7, 10, 15)]

    def test_each_window_holds_its_own_quota_slot(self, store: Store) -> None:
        market = _market(store, fired=(7, 5, 3))
        _engine(store).decide(market, NOW)
        assert market.reservations == {3, 5, 7}


class TestWindowsFiringOverSeparatePasses:
    def test_a_later_pass_decides_a_newly_fired_window(self, store: Store) -> None:
        """Level-triggered: windows fire as the TWAP crosses each trigger, so the
        ordinary case is one new window per pass, not five at once."""
        engine = _engine(store)
        market = _market(store, fired=(15,))
        assert [i.offset_seconds for i in engine.decide(market, NOW).intents] == [15]

        market.window(10).mark_fired(NOW + 5.0)
        assert [i.offset_seconds for i in engine.decide(market, NOW).intents] == [10]

        market.window(3).mark_fired(NOW + 12.0)
        assert [i.offset_seconds for i in engine.decide(market, NOW).intents] == [3]

        assert len(store.intents_for(market.slug)) == 3
        assert engine.intents_created == 3

    def test_an_already_decided_window_is_denied_not_re_decided(
        self, store: Store
    ) -> None:
        engine = _engine(store)
        market = _market(store, fired=(5, 3))
        engine.decide(market, NOW)
        outcome = engine.decide(market, NOW)
        assert outcome.intents == ()
        assert {d.denial for d in outcome.denials} == {DenialReason.DUPLICATE_INTENT}

    def test_twenty_passes_produce_one_intent_per_fired_window(
        self, store: Store
    ) -> None:
        engine = _engine(store)
        market = _market(store, fired=(15, 10, 7, 5, 3))
        for _ in range(20):
            engine.decide(market, NOW)
        assert len(store.intents_for(market.slug)) == 5
        assert len(market.intents) == 5
        assert engine.intents_created == 5


class TestTheQuotaBoundsIndependence:
    def test_the_quota_stops_the_sixth_window(self, store: Store) -> None:
        """Independence is bounded by the budget, not unbounded. Five fired windows
        against a three-trade limit must produce three intents."""
        market = _market(store, fired=(15, 10, 7, 5, 3))
        outcome = _engine(store, limit=3).decide(market, NOW)
        assert [i.offset_seconds for i in outcome.intents] == [3, 5, 7]
        assert {d.denial for d in outcome.denials} == {DenialReason.TRADE_QUOTA_EXHAUSTED}

    def test_the_quota_refusal_names_the_budget(self, store: Store) -> None:
        market = _market(store, fired=(15, 10, 7, 5, 3))
        outcome = _engine(store, limit=3).decide(market, NOW)
        (denied,) = [d for d in outcome.denials if d.offset_seconds == 10]
        assert "of 3" in denied.detail

    def test_reservations_are_what_bound_it_before_any_fill(self, store: Store) -> None:
        """H2. Without reservations all five windows would pass a used-only check in
        the same pass, because none of them has filled yet."""
        market = _market(store, fired=(15, 10, 7, 5, 3))
        _engine(store, limit=2).decide(market, NOW)
        assert market.reservations == {3, 5}
        assert market.fills == []

    def test_a_filled_window_keeps_its_slot_rather_than_freeing_one(
        self, store: Store
    ) -> None:
        """A fill moves the slot from reserved to used, not back to available."""
        engine = _engine(store, limit=2)
        market = _market(store, fired=(15, 10, 7, 5, 3))
        engine.decide(market, NOW)
        fill_window(market, 3, size=Decimal("35"))
        outcome = engine.decide(market, NOW)
        assert outcome.intents == ()
        assert DenialReason.TRADE_QUOTA_EXHAUSTED in {d.denial for d in outcome.denials}

    def test_the_budget_resets_at_a_market_boundary(self, store: Store) -> None:
        """A11: a new market is a new object, so its reservations start empty. A budget
        that carried over would stop trading after the first market."""
        engine = _engine(store, limit=2)
        first = _market(store, fired=(5, 3), window_ts=1754400000)
        second = _market(store, fired=(5, 3), window_ts=1754400300)
        assert len(engine.decide(first, NOW).intents) == 2
        assert len(engine.decide(second, NOW).intents) == 2
        assert engine.intents_created == 4


class TestOpposingDirectionsAreStillBlocked:
    def _split_market(self, store: Store) -> MarketInstance:
        """A market whose 3s window froze above the PTB and whose 5s window froze below.

        This is a real configuration, not a contrived one: each window captures its own
        opening TWAP at its own freeze instant, and `direction` is derived from that
        window's opening TWAP against the shared PTB. A market whose TWAP crosses the
        PTB between two freezes therefore holds windows of opposite direction.
        """
        market = MarketInstance.create(1754400000, (5, 3))
        market.phase = MarketPhase.ACTIVE
        market.freeze_ptb(BASE_PTB)
        # 5s froze below the PTB -> DOWN.
        market.window(5).freeze(
            opening_twap=BASE_PTB - Decimal("50"),
            ptb=BASE_PTB,
            buffer=Decimal("1.00"),
            frozen_at=float(1754400000),
        )
        # 3s froze above it -> UP.
        market.window(3).freeze(
            opening_twap=BASE_PTB + Decimal("50"),
            ptb=BASE_PTB,
            buffer=Decimal("1.00"),
            frozen_at=float(1754400000),
        )
        assert market.window(3).direction is Direction.UP
        assert market.window(5).direction is Direction.DOWN
        market.accumulator.add(BASE_PTB + Decimal("500"))
        for offset in (5, 3):
            market.window(offset).mark_fired(NOW)
        store.create_market(market, NOW)
        return market

    def test_a_held_direction_blocks_the_other_side(self, store: Store) -> None:
        """H3: UP at 0.79 plus DOWN at 0.22 costs 1.01 and returns 1.00 — a guaranteed
        loss. Multi-trade means several windows, not both sides."""
        engine = _engine(store)
        market = self._split_market(store)
        fill_window(market, 3, size=Decimal("35"))
        assert market.directions_held() == frozenset({Direction.UP})
        outcome = engine.decide(market, NOW)
        (denied,) = [d for d in outcome.denials if d.offset_seconds == 5]
        assert denied.denial is DenialReason.OPPOSING_DIRECTION_BLOCKED

    def test_the_same_side_window_still_trades(self, store: Store) -> None:
        """The gate blocks the opposite side only. Blocking both would stop trading
        entirely as soon as a single position existed."""
        engine = _engine(store)
        market = self._split_market(store)
        fill_window(market, 3, size=Decimal("35"))
        outcome = engine.decide(market, NOW)
        assert [i.offset_seconds for i in outcome.intents] == [3]
        assert outcome.intents[0].direction is Direction.UP

    def test_nothing_is_blocked_before_a_position_is_actually_held(
        self, store: Store
    ) -> None:
        """An unfilled order is not a position. Treating one as held would block the
        opposite side for a trade that never happened."""
        market = self._split_market(store)
        assert market.directions_held() == frozenset()
        outcome = _engine(store).decide(market, NOW)
        assert len(outcome.intents) == 2
        assert {i.direction for i in outcome.intents} == {Direction.UP, Direction.DOWN}


class TestModeIsDerivedFromTheLimitAlone:
    def test_a_limit_of_one_is_single_trade(self, store: Store) -> None:
        market = _market(store, fired=(5, 3))
        assert len(_engine(store, limit=1).decide(market, NOW).intents) == 1

    def test_a_limit_above_one_is_multi_trade(self, store: Store) -> None:
        market = _market(store, fired=(5, 3))
        assert len(_engine(store, limit=2).decide(market, NOW).intents) == 2

    def test_there_is_no_second_flag_that_could_disagree(self) -> None:
        """"multi trade enabled, max trades 1" would have no correct reading."""
        from arc.config import TradingConfig
        from arc.risk.limits import RiskLimits

        for cls in (TradingConfig, RiskLimits):
            names = {f.name for f in cls.__dataclass_fields__.values()}
            assert not any("multi_trade" in name or "multi trade" in name for name in names)
