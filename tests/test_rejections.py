"""Every rejection the Decision Engine can produce, driven end to end.

test_risk.py evaluates the gates directly. This file drives the whole engine and
asserts that each condition an operator can actually create reaches the log with its
own reason — because a gate that is correct in isolation and unreachable in practice
protects nothing.

Three of the fourteen gates are defence in depth and cannot be reached through the
engine at all; the last class documents why, and asserts it, rather than leaving the
absence of a test to look like an oversight.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from decision_fixtures import BASE_PTB, fill_window, fired_market, healthy, make_engine, trading

from arc.decision.engine import RuntimeHealth
from arc.decision.reasons import SkipReason
from arc.domain.enums import (
    DenialReason,
    Direction,
    MarketPhase,
    SettlementSpecStatus,
    WindowState,
)
from arc.domain.models import MarketInstance
from arc.risk.engine import GATE_ORDER
from arc.storage.store import Store

NOW = 1754400001.0


@pytest.fixture
def market(store: Store) -> MarketInstance:
    instance = fired_market()
    store.create_market(instance, NOW)
    return instance


def _denial(
    store: Store,
    market: MarketInstance,
    *,
    health: RuntimeHealth | None = None,
    quote_price: Decimal | None = Decimal("0.70"),
    **config: str,
) -> tuple[DenialReason, str, str]:
    """Run one pass and return the 3s window's (reason, gate, detail).

    A helper rather than a fixture so each test states exactly the one condition it
    varies and inherits a valid everything-else.
    """
    engine = make_engine(
        store,
        config=trading(**config) if config else None,
        health=health,
        quote_price=quote_price,
    )
    outcome = engine.decide(market, NOW)
    assert outcome.intents == (), "expected no intent"
    (decision,) = [d for d in outcome.decisions if d.offset_seconds == 3]
    assert decision.denial is not None, f"expected a denial, got skip={decision.skip}"
    return decision.denial, decision.gate, decision.detail


class TestGate1TradingEnabled:
    def test_an_unverified_settlement_spec_refuses_every_trade(
        self, store: Store, market: MarketInstance
    ) -> None:
        """A8. THE PROCESS ALWAYS STARTS: feeds run, windows fire, decisions are
        evaluated — and nothing is ever submitted. Enforced at the authorisation
        boundary, so a caller who forgets to check the flag still cannot submit."""
        reason, gate, detail = _denial(
            store,
            market,
            health=healthy(spec_status=SettlementSpecStatus.UNVERIFIED),
        )
        assert reason is DenialReason.TRADING_DISABLED_SPEC_UNVERIFIED
        assert gate == "trading_enabled"
        assert "UNVERIFIED" in detail

    def test_the_shipped_default_refuses(self, store: Store, market: MarketInstance) -> None:
        """UNVERIFIED is the startup value. A fresh install must not trade."""
        default = RuntimeHealth(
            trading_enabled=True, spec_status=SettlementSpecStatus.UNVERIFIED
        )
        reason, _, _ = _denial(store, market, health=default)
        assert reason is DenialReason.TRADING_DISABLED_SPEC_UNVERIFIED

    def test_an_operator_pause_has_its_own_reason(
        self, store: Store, market: MarketInstance
    ) -> None:
        """Distinct from the spec gate: one is a deliberate operator action and the
        other is an unanswered question about the venue."""
        reason, gate, detail = _denial(store, market, health=healthy(paused=True))
        assert reason is DenialReason.TRADING_PAUSED
        assert gate == "trading_enabled"
        assert "paused" in detail

    def test_trading_disabled_carries_the_supplied_reason(
        self, store: Store, market: MarketInstance
    ) -> None:
        reason, _, detail = _denial(
            store,
            market,
            health=healthy(trading_enabled=False, trading_disabled_reason="watchdog tripped"),
        )
        assert reason is DenialReason.TRADING_DISABLED_SPEC_UNVERIFIED
        assert detail == "watchdog tripped"

    def test_nothing_is_persisted_when_trading_is_disabled(
        self, store: Store, market: MarketInstance
    ) -> None:
        """A8 says decisions are RECORDED but never submitted. An intent row IS an
        authorisation to submit, so a denied window must not leave one."""
        _denial(store, market, health=healthy(paused=True))
        assert not store.has_intent(market.slug, 3)
        assert market.reservations == set()
        assert market.intents == []


class TestGate2MarketPhase:
    def test_a_cancelling_market_refuses(self, store: Store, market: MarketInstance) -> None:
        """A10/D1: the phase is the ONLY execution boundary. There is no lead-time
        gate, and nothing here reads a clock to decide whether an action is too late."""
        market.phase = MarketPhase.CANCELLING
        reason, gate, _ = _denial(store, market)
        assert reason is DenialReason.MARKET_CANCELLING
        assert gate == "market_phase"

    def test_a_dead_market_refuses_with_its_own_reason(
        self, store: Store, market: MarketInstance
    ) -> None:
        """A1 Rule 1: no official PTB means the market is never traded. It must be
        distinguishable in the log from the ordinary end-of-market cancel."""
        market.phase = MarketPhase.DEAD
        reason, _, detail = _denial(store, market)
        assert reason is DenialReason.MARKET_DEAD
        assert "PTB" in detail

    @pytest.mark.parametrize(
        "phase", [MarketPhase.DISCOVERED, MarketPhase.SETTLING, MarketPhase.SETTLED]
    )
    def test_any_other_phase_refuses(
        self, store: Store, market: MarketInstance, phase: MarketPhase
    ) -> None:
        market.phase = phase
        reason, _, detail = _denial(store, market)
        assert reason is DenialReason.MARKET_NOT_ACTIVE
        assert phase.value in detail


class TestGate6DuplicateIntent:
    def test_a_window_that_already_decided_is_refused(
        self, store: Store, market: MarketInstance
    ) -> None:
        """A12: exactly one intent per window, ever."""
        make_engine(store).decide(market, NOW)
        reason, gate, _ = _denial(store, market)
        assert reason is DenialReason.DUPLICATE_INTENT
        assert gate == "duplicate_intent"

    def test_it_survives_a_process_restart(self, store: Store, market: MarketInstance) -> None:
        """The authority is the SQLite constraint, not an in-memory set, so a crash
        between the decision and the submission cannot produce a second order."""
        make_engine(store).decide(market, NOW)
        fresh_market = fired_market()
        reason, _, _ = _denial(store, fresh_market)
        assert reason is DenialReason.DUPLICATE_INTENT
        assert fresh_market.intents == []


class TestGate7TradeQuota:
    def test_an_exhausted_budget_refuses(self, store: Store) -> None:
        market = fired_market(fired=(5, 3))
        store.create_market(market, NOW)
        engine = make_engine(store, config=trading(max_trades_per_market="2"))
        engine.decide(market, NOW)
        # Both slots are now reserved; a third window firing must be refused.
        market.window(7).mark_fired(NOW + 1.0)
        outcome = engine.decide(market, NOW)
        (decision,) = [d for d in outcome.decisions if d.offset_seconds == 7]
        assert decision.denial is DenialReason.TRADE_QUOTA_EXHAUSTED
        assert decision.gate == "trade_quota"

    def test_the_detail_separates_used_from_reserved(self, store: Store) -> None:
        """H2. An operator reading "2 used of 2" when nothing has filled would go
        looking for two fills that do not exist."""
        market = fired_market(fired=(5, 3))
        store.create_market(market, NOW)
        engine = make_engine(store, config=trading(max_trades_per_market="2"))
        engine.decide(market, NOW)
        market.window(7).mark_fired(NOW + 1.0)
        (decision,) = [
            d for d in engine.decide(market, NOW).decisions if d.offset_seconds == 7
        ]
        assert decision.detail == "0 used + 2 reserved of 2"

    def test_filled_trades_count_toward_the_same_budget(self, store: Store) -> None:
        market = fired_market(fired=(3,))
        store.create_market(market, NOW)
        engine = make_engine(store, config=trading(max_trades_per_market="1"))
        engine.decide(market, NOW)
        fill_window(market, 3, size=Decimal("35"))
        market.window(5).mark_fired(NOW + 1.0)
        (decision,) = [
            d for d in engine.decide(market, NOW).decisions if d.offset_seconds == 5
        ]
        assert decision.denial is DenialReason.TRADE_QUOTA_EXHAUSTED
        assert decision.detail == "1 used + 0 reserved of 1"


class TestGate8OpposingDirection:
    def _split_market(self, store: Store) -> MarketInstance:
        """Windows that froze on opposite sides of the PTB — a real configuration.

        Each window captures its own opening TWAP at its own freeze instant, so a
        market whose TWAP crosses the PTB between two freezes holds windows of
        opposite direction.
        """
        market = MarketInstance.create(1754400000, (5, 3))
        market.phase = MarketPhase.ACTIVE
        market.freeze_ptb(BASE_PTB)
        market.window(5).freeze(
            opening_twap=BASE_PTB - Decimal("50"),
            ptb=BASE_PTB,
            buffer=Decimal("1.00"),
            frozen_at=float(1754400000),
        )
        market.window(3).freeze(
            opening_twap=BASE_PTB + Decimal("50"),
            ptb=BASE_PTB,
            buffer=Decimal("1.00"),
            frozen_at=float(1754400000),
        )
        market.accumulator.add(BASE_PTB + Decimal("500"))
        for offset in (5, 3):
            market.window(offset).mark_fired(NOW)
        store.create_market(market, NOW)
        return market

    def test_the_opposite_side_is_refused_when_a_position_is_held(
        self, store: Store
    ) -> None:
        """H3: UP at 0.79 plus DOWN at 0.22 costs 1.01 and returns exactly 1.00. It
        is not a hedge, it is a fee."""
        market = self._split_market(store)
        fill_window(market, 3, size=Decimal("35"))
        outcome = make_engine(store).decide(market, NOW)
        (decision,) = [d for d in outcome.decisions if d.offset_seconds == 5]
        assert decision.denial is DenialReason.OPPOSING_DIRECTION_BLOCKED
        assert decision.gate == "opposing_direction"
        assert "UP is already held" in decision.detail

    def test_the_operator_can_enable_it_deliberately(self, store: Store) -> None:
        """Blocked by DEFAULT, not forbidden. The setting raises an advisory warning
        at config time rather than being silently unavailable."""
        market = self._split_market(store)
        fill_window(market, 3, size=Decimal("35"))
        engine = make_engine(store, config=trading(allow_opposing_directions="true"))
        outcome = engine.decide(market, NOW)
        assert [i.offset_seconds for i in outcome.intents] == [3, 5]
        assert {i.direction for i in outcome.intents} == {Direction.UP, Direction.DOWN}
        assert outcome.denials == ()


class TestGate9PositionLimit:
    def test_the_process_wide_limit_refuses(
        self, store: Store, market: MarketInstance
    ) -> None:
        """D6: two markets are live at every boundary, so a per-market budget alone
        permits twice the intended exposure in exactly the seconds late windows
        trade in."""
        reason, gate, detail = _denial(store, market, health=healthy(open_positions=3))
        assert reason is DenialReason.POSITION_LIMIT_REACHED
        assert gate == "position_limit"
        assert detail == "3 open of 3"

    def test_one_below_the_limit_still_trades(self, store: Store, market: MarketInstance) -> None:
        engine = make_engine(store, health=healthy(open_positions=2))
        assert len(engine.decide(market, NOW).intents) == 1


class TestGate10EntryBand:
    def test_a_price_above_the_band_is_refused(
        self, store: Store, market: MarketInstance
    ) -> None:
        """Too expensive is a trade not worth taking."""
        reason, gate, detail = _denial(store, market, quote_price=Decimal("0.95"))
        assert reason is DenialReason.ENTRY_PRICE_LIMIT
        assert gate == "entry_band"
        assert detail == "0.95 > 0.85"

    def test_a_price_below_the_band_has_its_own_reason(
        self, store: Store, market: MarketInstance
    ) -> None:
        """Too cheap means the book disagrees with the signal — the opposite problem,
        and it must not be logged as the same one."""
        reason, _, detail = _denial(store, market, quote_price=Decimal("0.20"))
        assert reason is DenialReason.ENTRY_PRICE_BELOW_MIN
        assert detail == "0.20 < 0.55"

    def test_the_price_is_floored_before_it_is_validated(
        self, store: Store, market: MarketInstance
    ) -> None:
        """D2: 0.857 floors to 0.85 and is admissible. Validating first and flooring
        second would reject a price the venue would have accepted."""
        engine = make_engine(store, quote_price=Decimal("0.857"))
        (intent,) = engine.decide(market, NOW).intents
        assert intent.limit_price == Decimal("0.85")

    @pytest.mark.parametrize("price", ["0.55", "0.85"])
    def test_both_edges_of_the_band_are_admissible(
        self, store: Store, market: MarketInstance, price: str
    ) -> None:
        engine = make_engine(store, quote_price=Decimal(price))
        (intent,) = engine.decide(market, NOW).intents
        assert intent.limit_price == Decimal(price)


class TestGate11ExchangeMinimum:
    """The reachable case is narrow, and that is the point.

    Config invariant 8 already refuses a budget that cannot buy the minimum at the top
    of the band, so a grossly undersized configuration never starts. What survives that
    check is the flooring case (D2): the budget affords 29.41 shares at 0.85, the size
    is floored to 29, and 29 is below a 29.4 minimum. This is the only way the gate
    fires in production, and it is exactly why the gate exists behind the invariant.
    """

    CONFIG = {"min_tradable_size": "29.4", "position_notional_usd": "25.00"}

    def test_a_floored_size_below_the_venue_minimum_is_refused(
        self, store: Store, market: MarketInstance
    ) -> None:
        reason, gate, detail = _denial(
            store, market, quote_price=Decimal("0.85"), **self.CONFIG
        )
        assert reason is DenialReason.SIZE_BELOW_EXCHANGE_MINIMUM
        assert gate == "exchange_minimum"
        assert detail == "29 < 29.4"

    def test_it_is_a_denial_and_not_a_skip(
        self, store: Store, market: MarketInstance
    ) -> None:
        """The gates run before the strategy's act flag is consulted, so a trade
        refused by venue policy reports the policy an operator can change — not a
        sizing note they cannot."""
        engine = make_engine(
            store, config=trading(**self.CONFIG), quote_price=Decimal("0.85")
        )
        (decision,) = [
            d for d in engine.decide(market, NOW).decisions if d.offset_seconds == 3
        ]
        assert decision.skip is None
        assert decision.denial is DenialReason.SIZE_BELOW_EXCHANGE_MINIMUM

    def test_a_cheaper_quote_under_the_same_config_still_trades(
        self, store: Store, market: MarketInstance
    ) -> None:
        """0.70 affords 35 shares, which clears the same 29.4 minimum. The gate is
        about the size actually computed, not about the configuration."""
        engine = make_engine(
            store, config=trading(**self.CONFIG), quote_price=Decimal("0.70")
        )
        (intent,) = engine.decide(market, NOW).intents
        assert intent.size == Decimal("35")


class TestGate12LossLimits:
    def test_the_daily_loss_limit_refuses(self, store: Store, market: MarketInstance) -> None:
        reason, gate, detail = _denial(
            store, market, health=healthy(daily_loss_usd=Decimal("50.00"))
        )
        assert reason is DenialReason.LOSS_LIMIT_REACHED
        assert gate == "loss_limits"
        assert detail == "daily loss 50.00 reached 50.00"

    def test_the_consecutive_loss_limit_refuses(
        self, store: Store, market: MarketInstance
    ) -> None:
        """A separate threshold from the daily total, because a losing streak and a
        single large loss are separate signals."""
        reason, _, detail = _denial(store, market, health=healthy(consecutive_losses=5))
        assert reason is DenialReason.LOSS_LIMIT_REACHED
        assert detail == "5 consecutive losses reached 5"

    def test_a_zero_limit_disables_that_check(self, store: Store, market: MarketInstance) -> None:
        """Zero means "no limit", not "refuse everything" — otherwise the documented
        way to disable a limit would stop all trading."""
        engine = make_engine(
            store,
            config=trading(max_daily_loss_usd="0.00", max_consecutive_losses="0"),
            health=healthy(daily_loss_usd=Decimal("9999.00"), consecutive_losses=99),
        )
        assert len(engine.decide(market, NOW).intents) == 1


class TestGate13FeedFreshness:
    def test_a_blocked_feed_refuses(self, store: Store, market: MarketInstance) -> None:
        """The quiet failure: the socket is up, the process is healthy, the last
        observation is forty seconds old, and the TWAP describes a market that has
        moved. Nothing raises — so freshness is a gate, not a warning."""
        reason, gate, detail = _denial(
            store, market, health=healthy(feed_blocked=True, feed_age_ms=41000.0)
        )
        assert reason is DenialReason.FEED_STALE
        assert gate == "feed_freshness"
        assert detail == "last observation 41000ms ago"

    def test_a_feed_that_never_delivered_says_never(
        self, store: Store, market: MarketInstance
    ) -> None:
        """Not "0ms ago", which would read as the freshest possible feed."""
        _, _, detail = _denial(
            store, market, health=healthy(feed_blocked=True, feed_age_ms=None)
        )
        assert detail == "last observation never ago"


class TestGate14RuntimeHealth:
    def test_critical_clock_drift_refuses(self, store: Store, market: MarketInstance) -> None:
        """On a three-second window, drift near a one-second threshold consumes a
        third of the window: the process would compute a window_ts for the wrong
        market and freeze against the wrong market's opening TWAP."""
        reason, gate, detail = _denial(
            store,
            market,
            health=healthy(clock_drift_critical=True, clock_drift_ms=-1400.0),
        )
        assert reason is DenialReason.CLOCK_DRIFT_CRITICAL
        assert gate == "runtime_health"
        assert "-1400ms" in detail

    def test_an_unhealthy_runtime_refuses(self, store: Store, market: MarketInstance) -> None:
        reason, _, detail = _denial(
            store, market, health=healthy(healthy=False, detail="observation loop stalled")
        )
        assert reason is DenialReason.RUNTIME_UNHEALTHY
        assert detail == "observation loop stalled"


class TestSkipsAreNotDenials:
    def test_no_quote_is_a_skip(self, store: Store, market: MarketInstance) -> None:
        """No book is not a policy refusal. Logging it as one would send an operator
        looking for a limit to raise."""
        outcome = make_engine(store, quote_price=None).decide(market, NOW)
        assert outcome.denials == ()
        assert outcome.decisions[0].skip is SkipReason.NO_QUOTE

    def test_an_unfired_window_is_a_skip(self, store: Store) -> None:
        instance = fired_market(fired=())
        store.create_market(instance, NOW)
        outcome = make_engine(store).decide(instance, NOW)
        assert outcome.denials == ()
        assert all(d.skip is SkipReason.NOT_FIRED for d in outcome.decisions)


class TestTheFirstBrokenGateWins:
    def test_the_earliest_condition_is_the_one_reported(
        self, store: Store, market: MarketInstance
    ) -> None:
        """Every gate is broken at once. The reported reason must be the first in
        GATE_ORDER, so the same situation always reports the same reason and a change
        in the log means a change in the world."""
        market.phase = MarketPhase.CANCELLING
        reason, gate, _ = _denial(
            store,
            market,
            health=healthy(
                spec_status=SettlementSpecStatus.UNVERIFIED,
                paused=True,
                open_positions=99,
                daily_loss_usd=Decimal("9999.00"),
                consecutive_losses=99,
                feed_blocked=True,
                clock_drift_critical=True,
                healthy=False,
            ),
            quote_price=Decimal("0.99"),
        )
        assert gate == GATE_ORDER[0]
        assert reason is DenialReason.TRADING_DISABLED_SPEC_UNVERIFIED

    def test_the_phase_gate_wins_over_every_later_one(
        self, store: Store, market: MarketInstance
    ) -> None:
        market.phase = MarketPhase.CANCELLING
        _, gate, _ = _denial(
            store,
            market,
            health=healthy(open_positions=99, feed_blocked=True, healthy=False),
            quote_price=Decimal("0.99"),
        )
        assert gate == "market_phase"


class TestTheGatesThatCannotBeReachedFromHere:
    """Three gates are structurally unreachable through the Decision Engine.

    They are defence in depth, not dead code: they exist so a future caller that
    reaches the Risk Engine by another path cannot authorise a trade the engine's own
    validation would have caught. test_risk.py evaluates them directly. This class
    asserts the structural reason each one cannot fire here, so the missing
    end-to-end test is a documented consequence rather than a gap.
    """

    def test_window_triggered_cannot_fail_because_only_fired_windows_are_decided(
        self, store: Store
    ) -> None:
        instance = fired_market(fired=(3,))
        store.create_market(instance, NOW)
        outcome = make_engine(store).decide(instance, NOW)
        decided = [d for d in outcome.decisions if d.denial is not None or d.acted]
        assert all(instance.window(d.offset_seconds).state is WindowState.FIRED
                   for d in decided)

    def test_price_to_beat_cannot_fail_because_no_snapshot_exists_without_one(
        self, store: Store
    ) -> None:
        instance = MarketInstance.create(1754400000, (3,))
        instance.phase = MarketPhase.ACTIVE
        instance.accumulator.add(BASE_PTB)
        instance.window(3).freeze(
            opening_twap=BASE_PTB + Decimal("100"),
            ptb=BASE_PTB,
            buffer=Decimal("1.00"),
            frozen_at=float(1754400000),
        )
        instance.window(3).mark_fired(NOW)
        store.create_market(instance, NOW)
        assert instance.ptb is None
        outcome = make_engine(store).decide(instance, NOW)
        assert outcome.denials == ()
        assert outcome.decisions[0].skip is SkipReason.INCOMPLETE

    def test_strategy_enabled_cannot_fail_because_the_default_is_pinned(self) -> None:
        """A17: the one strategy is pinned and not disableable, so the registry cannot
        be emptied. An empty registry would skip every fired window in silence, which
        is exactly what gate 5 exists to name if it ever becomes possible."""
        from arc.strategy.registry import DEFAULT_STRATEGY_ID, default_registry

        registry = default_registry()
        with pytest.raises(ValueError, match="pinned"):
            registry.unregister(DEFAULT_STRATEGY_ID)
        assert len(registry) == 1


class TestEveryGateIsAccountedFor:
    def test_the_inventory_matches_the_engine(self) -> None:
        """Union of the frozen specifications: fourteen distinct gates, no merges."""
        assert len(GATE_ORDER) == 14
        assert len(set(GATE_ORDER)) == 14

    def test_every_gate_is_either_exercised_here_or_documented_unreachable(self) -> None:
        exercised = {
            "trading_enabled",
            "market_phase",
            "duplicate_intent",
            "trade_quota",
            "opposing_direction",
            "position_limit",
            "entry_band",
            "exchange_minimum",
            "loss_limits",
            "feed_freshness",
            "runtime_health",
        }
        unreachable = {"window_triggered", "price_to_beat", "strategy_enabled"}
        assert exercised | unreachable == set(GATE_ORDER)
        assert exercised.isdisjoint(unreachable)

    def test_no_repealed_lead_time_reason_exists(self) -> None:
        """D1 repealed entirely. MARKET_CANCELLING is a PHASE gate, not a timing one."""
        assert not hasattr(DenialReason, "INSUFFICIENT_LEAD_TIME")
        assert not any("LEAD_TIME" in member.value for member in DenialReason)
