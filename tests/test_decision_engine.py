"""The Decision Engine's six-step pipeline.

Every test drives the real engine with the real risk engine, the real registry, the
real strategy and a real on-disk store. Only the book quote and the process health
readings are supplied, and both are genuinely external inputs.
"""

from __future__ import annotations

import logging
from decimal import Decimal

import pytest
from decision_fixtures import fired_market, healthy, make_engine

from arc.decision.engine import DecisionEngine, DecisionOutcome, RuntimeHealth
from arc.decision.reasons import SkipReason
from arc.domain.enums import DenialReason, Direction, MarketPhase, WindowState
from arc.domain.models import MarketInstance
from arc.storage.store import Store

NOW = 1754400001.0


@pytest.fixture
def market(store: Store) -> MarketInstance:
    """A market whose 3s window has fired, with its parent row on disk.

    The row is required: intents carry a foreign key onto markets(slug), which is
    what stops an intent existing for a market the process never opened.
    """
    instance = fired_market()
    store.create_market(instance, NOW)
    return instance


@pytest.fixture
def engine(store: Store) -> DecisionEngine:
    return make_engine(store)


def _decide(engine: DecisionEngine, market: MarketInstance) -> DecisionOutcome:
    return engine.decide(market, NOW)


class TestTheHappyPath:
    def test_a_fired_window_produces_one_intent(
        self, engine: DecisionEngine, market: MarketInstance
    ) -> None:
        outcome = _decide(engine, market)
        assert len(outcome.intents) == 1
        assert outcome.intents[0].offset_seconds == 3

    def test_the_intent_carries_the_windows_frozen_values(
        self, engine: DecisionEngine, market: MarketInstance
    ) -> None:
        (intent,) = _decide(engine, market).intents
        window = market.window(3)
        assert intent.direction == window.direction
        assert intent.locked_trigger == window.locked_trigger
        assert intent.opening_twap == window.opening_twap
        assert intent.buffer == window.buffer
        assert intent.ptb == market.ptb

    def test_the_intent_is_priced_and_sized(
        self, engine: DecisionEngine, market: MarketInstance
    ) -> None:
        """25.00 of budget at a 0.70 quote is 35 whole shares."""
        (intent,) = _decide(engine, market).intents
        assert intent.limit_price == Decimal("0.70")
        assert intent.size == Decimal("35")

    def test_the_intent_names_the_shipped_strategy(
        self, engine: DecisionEngine, market: MarketInstance
    ) -> None:
        (intent,) = _decide(engine, market).intents
        assert intent.strategy_id == "arc_twap_locked_buffer"

    def test_created_at_is_the_value_passed_in(
        self, engine: DecisionEngine, market: MarketInstance
    ) -> None:
        """Passed, never read. The engine has no clock, so admissibility cannot
        depend on how long the pass took (A10/D1)."""
        (intent,) = _decide(engine, market).intents
        assert intent.created_at == NOW

    def test_the_intent_is_persisted(
        self, engine: DecisionEngine, market: MarketInstance, store: Store
    ) -> None:
        _decide(engine, market)
        assert store.has_intent(market.slug, 3)
        assert len(store.intents_for(market.slug)) == 1

    def test_the_intent_is_attached_to_the_market(
        self, engine: DecisionEngine, market: MarketInstance
    ) -> None:
        _decide(engine, market)
        assert len(market.intents) == 1
        assert market.intents[0].offset_seconds == 3

    def test_a_quota_slot_is_reserved(
        self, engine: DecisionEngine, market: MarketInstance
    ) -> None:
        """H2: the slot is held from admission, before any fill, so two windows in
        the same second cannot both pass a used-only check."""
        _decide(engine, market)
        assert market.reservations == {3}

    def test_the_created_counter_advances(
        self, engine: DecisionEngine, market: MarketInstance
    ) -> None:
        _decide(engine, market)
        assert engine.intents_created == 1
        assert engine.intents_denied == 0

    def test_both_directions_produce_a_correct_intent(self, store: Store) -> None:
        """A12: UP fires on >= and DOWN on <=. A shared comparison would delete half
        the strategy, and it would still produce intents — just wrong ones."""
        engine = make_engine(store)
        for direction in (Direction.UP, Direction.DOWN):
            instance = fired_market(direction=direction, window_ts=1754400000 + 300 * (
                0 if direction is Direction.UP else 1
            ))
            store.create_market(instance, NOW)
            (intent,) = _decide(engine, instance).intents
            assert intent.direction is direction
            if direction is Direction.UP:
                assert intent.signal_twap >= intent.locked_trigger
                assert intent.locked_trigger > intent.opening_twap
            else:
                assert intent.signal_twap <= intent.locked_trigger
                assert intent.locked_trigger < intent.opening_twap


class TestIdempotence:
    def test_a_second_pass_creates_nothing_more(
        self, engine: DecisionEngine, market: MarketInstance
    ) -> None:
        """Level-triggered: the pass runs on every accepted observation, so a second
        call on the same fired window must be a no-op rather than a second order."""
        _decide(engine, market)
        second = _decide(engine, market)
        assert second.intents == ()
        assert engine.intents_created == 1

    def test_the_second_pass_reports_a_duplicate_denial(
        self, engine: DecisionEngine, market: MarketInstance
    ) -> None:
        _decide(engine, market)
        (denied,) = _decide(engine, market).denials
        assert denied.denial is DenialReason.DUPLICATE_INTENT

    def test_twenty_passes_leave_exactly_one_row(
        self, engine: DecisionEngine, market: MarketInstance, store: Store
    ) -> None:
        for _ in range(20):
            _decide(engine, market)
        assert len(store.intents_for(market.slug)) == 1
        assert len(market.intents) == 1

    def test_a_restart_does_not_re_decide_a_persisted_window(
        self, store: Store, market: MarketInstance
    ) -> None:
        """A4: the intent is on disk, so a fresh process must recognise it rather than
        submit the window again."""
        make_engine(store).decide(market, NOW)
        restarted = make_engine(store)
        outcome = restarted.decide(fired_market(), NOW)
        assert outcome.intents == ()
        assert outcome.denials[0].denial is DenialReason.DUPLICATE_INTENT


class TestWhatIsNotDecided:
    def test_an_unfrozen_window_is_skipped_not_denied(
        self, engine: DecisionEngine, store: Store
    ) -> None:
        instance = MarketInstance.create(1754400000, (3, 5))
        instance.phase = MarketPhase.ACTIVE
        store.create_market(instance, NOW)
        outcome = engine.decide(instance, NOW)
        assert outcome.intents == ()
        assert {d.skip for d in outcome.decisions} == {SkipReason.NOT_FIRED}

    def test_a_frozen_but_unfired_window_is_skipped(
        self, engine: DecisionEngine, store: Store
    ) -> None:
        """The ordinary case for four windows out of five. It must not appear in the
        rejection log, or the log becomes unreadable."""
        instance = fired_market(fired=())
        store.create_market(instance, NOW)
        outcome = engine.decide(instance, NOW)
        assert outcome.intents == ()
        assert outcome.denials == ()
        assert all(d.skip is SkipReason.NOT_FIRED for d in outcome.decisions)

    def test_only_the_fired_window_is_acted_on(
        self, engine: DecisionEngine, market: MarketInstance
    ) -> None:
        outcome = _decide(engine, market)
        acted = [d.offset_seconds for d in outcome.decisions if d.acted]
        assert acted == [3]
        assert len(outcome.decisions) == 5

    def test_no_quote_is_a_skip(self, store: Store, market: MarketInstance) -> None:
        """A skip, not a zero. A zero price would divide the budget by nothing."""
        engine = make_engine(store, quote_price=None)
        outcome = engine.decide(market, NOW)
        assert outcome.intents == ()
        assert outcome.decisions[0].skip is SkipReason.NO_QUOTE

    def test_a_zero_quote_is_a_skip(self, store: Store, market: MarketInstance) -> None:
        engine = make_engine(store, quote_price=Decimal("0"))
        assert engine.decide(market, NOW).decisions[0].skip is SkipReason.NO_QUOTE

    def test_a_skip_never_persists_anything(
        self, store: Store, market: MarketInstance
    ) -> None:
        make_engine(store, quote_price=None).decide(market, NOW)
        assert not store.has_intent(market.slug, 3)
        assert market.reservations == set()

    def test_exactly_one_outcome_is_set_per_window(
        self, engine: DecisionEngine, market: MarketInstance
    ) -> None:
        """A decision that carried both an intent and a denial would be handled twice
        by any caller that checked them independently."""
        for decision in _decide(engine, market).decisions:
            populated = sum(
                1
                for value in (decision.intent, decision.denial, decision.skip)
                if value is not None
            )
            assert populated == 1, decision


class TestTheEngineDoesNotMutateWhatItReads:
    def test_the_window_state_is_unchanged(
        self, engine: DecisionEngine, market: MarketInstance
    ) -> None:
        """The engine decides; the window engine owns state. An engine that could
        advance a window could fire one that never triggered."""
        before = {w.offset_seconds: w.state for w in market.windows_by_priority()}
        _decide(engine, market)
        after = {w.offset_seconds: w.state for w in market.windows_by_priority()}
        assert after == before
        assert after[3] is WindowState.FIRED

    def test_the_frozen_values_are_unchanged(
        self, engine: DecisionEngine, market: MarketInstance
    ) -> None:
        window = market.window(3)
        before = (
            window.opening_twap,
            window.ptb,
            window.buffer,
            window.direction,
            window.locked_trigger,
        )
        _decide(engine, market)
        assert (
            window.opening_twap,
            window.ptb,
            window.buffer,
            window.direction,
            window.locked_trigger,
        ) == before

    def test_the_twap_is_unchanged(
        self, engine: DecisionEngine, market: MarketInstance
    ) -> None:
        before = (market.accumulator.running_sum, market.accumulator.observation_count)
        _decide(engine, market)
        assert (market.accumulator.running_sum, market.accumulator.observation_count) == before

    def test_the_phase_is_unchanged(
        self, engine: DecisionEngine, market: MarketInstance
    ) -> None:
        _decide(engine, market)
        assert market.phase is MarketPhase.ACTIVE

    def test_no_order_and_no_fill_appears(
        self, engine: DecisionEngine, market: MarketInstance
    ) -> None:
        """The engine is a decision layer. An order here would be a submission from a
        component with no reconciliation path and no cancel path."""
        _decide(engine, market)
        assert market.orders == []
        assert market.fills == []

    def test_the_engine_has_no_venue_handle(self, engine: DecisionEngine) -> None:
        for slot in DecisionEngine.__slots__:
            assert not any(
                marker in slot for marker in ("client", "session", "wallet", "key", "http")
            ), slot


class TestPerMarketIsolation:
    def test_the_engine_holds_no_per_market_state(self, engine: DecisionEngine) -> None:
        """A11. Anything cached per market would carry one market's decision into the
        next and read as a correctly-enforced duplicate check."""
        for slot in DecisionEngine.__slots__:
            value = getattr(engine, slot)
            assert not isinstance(value, list | dict | set), slot

    def test_one_engine_serves_two_markets_alive_at_a_boundary(
        self, store: Store
    ) -> None:
        """D6: at most two MarketInstances are live and both must decide correctly."""
        engine = make_engine(store)
        closing = fired_market(window_ts=1754400000)
        current = fired_market(window_ts=1754400300)
        store.create_market(closing, NOW)
        store.create_market(current, NOW)
        assert len(engine.decide(closing, NOW).intents) == 1
        assert len(engine.decide(current, NOW).intents) == 1
        assert engine.intents_created == 2
        assert closing.slug != current.slug

    def test_the_counters_are_process_totals_not_per_market(
        self, store: Store
    ) -> None:
        engine = make_engine(store)
        for index in range(3):
            instance = fired_market(window_ts=1754400000 + 300 * index)
            store.create_market(instance, NOW)
            engine.decide(instance, NOW)
        assert engine.intents_created == 3


class TestLogging:
    def test_a_created_intent_is_logged_at_info(
        self, store: Store, market: MarketInstance, caplog: pytest.LogCaptureFixture
    ) -> None:
        logger = logging.getLogger("arc.test.decision.created")
        engine = make_engine(store, logger=logger)
        with caplog.at_level(logging.INFO, logger=logger.name):
            engine.decide(market, NOW)
        assert any("Intent Created" in record.message for record in caplog.records)

    def test_a_denial_is_logged_at_warning(
        self, store: Store, market: MarketInstance, caplog: pytest.LogCaptureFixture
    ) -> None:
        """An operator must be able to see a refusal without querying the database."""
        logger = logging.getLogger("arc.test.decision.denied")
        engine = make_engine(store, health=healthy(paused=True), logger=logger)
        with caplog.at_level(logging.WARNING, logger=logger.name):
            engine.decide(market, NOW)
        assert any("Intent Denied" in record.message for record in caplog.records)

    def test_a_skip_is_not_logged_as_a_denial(
        self, store: Store, caplog: pytest.LogCaptureFixture
    ) -> None:
        logger = logging.getLogger("arc.test.decision.skip")
        instance = fired_market(fired=())
        store.create_market(instance, NOW)
        with caplog.at_level(logging.DEBUG, logger=logger.name):
            make_engine(store, logger=logger).decide(instance, NOW)
        assert not any("Intent Denied" in record.message for record in caplog.records)


class TestTheHealthReadingIsTakenOnce:
    def test_one_reading_serves_the_whole_pass(self, store: Store) -> None:
        """Fifteen gates each pulling live readings would evaluate fifteen slightly
        different worlds, and the verdict would depend on how long evaluation took."""
        calls = 0

        def source() -> RuntimeHealth:
            nonlocal calls
            calls += 1
            return healthy()

        instance = fired_market(fired=(3, 5, 7))
        store.create_market(instance, NOW)
        make_engine(store, health_source=source).decide(instance, NOW)
        assert calls == 1
