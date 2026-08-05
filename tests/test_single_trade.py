"""Single-trade mode: one intent per market, priority 3 -> 5 -> 7 -> 10 -> 15.

Single-trade mode is `max_trades_per_market == 1`. There is no separate boolean: a
per-market limit of one IS single-trade mode, and a second flag could disagree with
the limit ("multi trade enabled, max trades 1") with no correct reading of that
configuration.
"""

from __future__ import annotations

from decision_fixtures import fired_market, healthy, make_engine, trading

from arc.decision.engine import DecisionEngine
from arc.decision.reasons import SkipReason
from arc.domain.enums import DenialReason
from arc.domain.models import MarketInstance
from arc.storage.store import Store

NOW = 1754400001.0
SINGLE = {"max_trades_per_market": "1"}


def _engine(store: Store) -> DecisionEngine:
    return make_engine(store, config=trading(**SINGLE))


def _market(store: Store, *, fired: tuple[int, ...]) -> MarketInstance:
    instance = fired_market(fired=fired)
    store.create_market(instance, NOW)
    return instance


class TestOneIntentPerMarket:
    def test_a_single_fired_window_still_trades(self, store: Store) -> None:
        market = _market(store, fired=(3,))
        assert len(_engine(store).decide(market, NOW).intents) == 1

    def test_five_fired_windows_produce_exactly_one_intent(self, store: Store) -> None:
        """The whole point of the mode. Five intents against a one-trade budget would
        be five positions on the same five-minute outcome."""
        market = _market(store, fired=(15, 10, 7, 5, 3))
        outcome = _engine(store).decide(market, NOW)
        assert len(outcome.intents) == 1
        assert len(store.intents_for(market.slug)) == 1

    def test_the_nearest_window_wins(self, store: Store) -> None:
        """3s is the best-informed window: it has watched the most of the TWAP. If a
        window that fired earlier took the budget, the mode would systematically trade
        on the least information available."""
        market = _market(store, fired=(15, 10, 7, 5, 3))
        (intent,) = _engine(store).decide(market, NOW).intents
        assert intent.offset_seconds == 3

    def test_the_nearest_window_wins_even_when_it_fired_last(self, store: Store) -> None:
        """Priority is by offset, not by firing order. A market where 15s fires first
        and 3s fires seconds later is the ordinary case, not the exception."""
        market = _market(store, fired=())
        market.window(15).mark_fired(NOW)
        market.window(3).mark_fired(NOW + 12.0)
        (intent,) = _engine(store).decide(market, NOW).intents
        assert intent.offset_seconds == 3
        assert market.window(15).fired_at is not None
        assert market.window(3).fired_at > market.window(15).fired_at

    def test_the_losers_are_skipped_not_denied(self, store: Store) -> None:
        """Spending the budget on a nearer window is the configured behaviour. Logging
        it as a risk denial would send an operator looking for a limit to raise."""
        market = _market(store, fired=(15, 10, 7, 5, 3))
        outcome = _engine(store).decide(market, NOW)
        assert outcome.denials == ()
        skipped = {d.offset_seconds: d.skip for d in outcome.decisions if not d.acted}
        assert skipped == dict.fromkeys((5, 7, 10, 15), SkipReason.LOWER_PRIORITY)

    def test_the_skip_says_why(self, store: Store) -> None:
        market = _market(store, fired=(5, 3))
        outcome = _engine(store).decide(market, NOW)
        (skipped,) = [d for d in outcome.decisions if d.skip is SkipReason.LOWER_PRIORITY]
        assert "higher-priority" in skipped.detail

    def test_the_skipped_counter_advances(self, store: Store) -> None:
        engine = _engine(store)
        engine.decide(_market(store, fired=(15, 10, 7, 5, 3)), NOW)
        assert engine.intents_created == 1
        assert engine.intents_skipped == 4

    def test_only_the_winner_holds_a_quota_slot(self, store: Store) -> None:
        """H2: four reservations against a one-trade budget would be permanently
        exhausted quota for four trades that were never admitted."""
        market = _market(store, fired=(15, 10, 7, 5, 3))
        _engine(store).decide(market, NOW)
        assert market.reservations == {3}


class TestPriorityRunsOverFiredWindowsOnly:
    def test_a_merely_frozen_nearer_window_does_not_take_the_budget(
        self, store: Store
    ) -> None:
        """A frozen window has not triggered. Letting it claim priority would leave the
        market untraded whenever the nearest window's trigger was never reached."""
        market = _market(store, fired=(5,))
        outcome = _engine(store).decide(market, NOW)
        assert [i.offset_seconds for i in outcome.intents] == [5]
        (three,) = [d for d in outcome.decisions if d.offset_seconds == 3]
        assert three.skip is SkipReason.NOT_FIRED

    def test_the_widest_window_can_win_when_it_is_the_only_one_fired(
        self, store: Store
    ) -> None:
        market = _market(store, fired=(15,))
        (intent,) = _engine(store).decide(market, NOW).intents
        assert intent.offset_seconds == 15


class TestWhenTheWinnerCannotTrade:
    def test_no_quote_produces_no_intent_and_no_denial(self, store: Store) -> None:
        market = _market(store, fired=(5, 3))
        engine = make_engine(store, config=trading(**SINGLE), quote_price=None)
        outcome = engine.decide(market, NOW)
        assert outcome.intents == ()
        assert outcome.denials == ()
        assert {d.skip for d in outcome.decisions if d.offset_seconds in (3, 5)} == {
            SkipReason.NO_QUOTE
        }

    def test_a_denied_nearest_window_does_not_hand_the_budget_on(
        self, store: Store
    ) -> None:
        """A process-wide denial — paused, stale feed, spec unverified — refuses every
        window. Passing the budget down after a denial would trade the 5s window under
        exactly the condition that just refused the 3s one."""
        market = _market(store, fired=(15, 10, 7, 5, 3))
        engine = make_engine(store, config=trading(**SINGLE), health=healthy(paused=True))
        outcome = engine.decide(market, NOW)
        assert outcome.intents == ()
        assert len(outcome.denials) == 5


class TestAcrossMarkets:
    def test_the_budget_is_per_market_not_per_process(self, store: Store) -> None:
        """One trade per market, and a market boundary is a fresh budget. A budget that
        carried over would stop trading after the first market of the day."""
        engine = _engine(store)
        for index in range(4):
            instance = fired_market(window_ts=1754400000 + 300 * index)
            store.create_market(instance, NOW)
            assert len(engine.decide(instance, NOW).intents) == 1
        assert engine.intents_created == 4

    def test_repeated_passes_over_one_market_stay_at_one_intent(
        self, store: Store
    ) -> None:
        engine = _engine(store)
        market = _market(store, fired=(15, 10, 7, 5, 3))
        for _ in range(10):
            engine.decide(market, NOW)
        assert len(store.intents_for(market.slug)) == 1
        assert engine.intents_created == 1

    def test_the_quota_gate_backs_the_priority_rule_on_a_later_pass(
        self, store: Store
    ) -> None:
        """The priority skip only holds within a single pass, because `already_acted`
        is pass-local. On the NEXT pass the 3s window is denied as a duplicate and the
        5s window reaches the gates — and it is the reservation from the first pass
        that refuses it. Without that reservation, a second pass over the same market
        would open a second position (hazard H2)."""
        engine = _engine(store)
        market = _market(store, fired=(5, 3))
        engine.decide(market, NOW)
        reasons = {d.offset_seconds: d.denial for d in engine.decide(market, NOW).denials}
        assert reasons[3] is DenialReason.DUPLICATE_INTENT
        assert reasons[5] is DenialReason.TRADE_QUOTA_EXHAUSTED
