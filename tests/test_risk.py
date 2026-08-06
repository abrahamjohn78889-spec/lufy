"""The Risk Engine: fifteen gates, one order, one reason each.

The union of every gate in the frozen specifications. Every gate gets its own test
here; what the Decision Engine does with a verdict is test_decision_engine.py's job,
and which reasons reach the log is test_rejections.py's.
"""

from __future__ import annotations

import ast
import inspect
from dataclasses import FrozenInstanceError
from decimal import Decimal
from pathlib import Path

import pytest
from conftest import VALID_TRADING_VALUES

from arc.config import build_trading_config
from arc.domain.enums import DenialReason, Direction, MarketPhase, SettlementSpecStatus
from arc.risk.engine import GATE_ORDER, RiskContext, RiskEngine
from arc.risk.limits import limits_from_trading

GATE_COUNT = 15


def _context(**overrides: object) -> RiskContext:
    """A context in which every gate passes. Each test breaks exactly one thing."""
    base: dict[str, object] = {
        "trading_enabled": True,
        "spec_status": SettlementSpecStatus.VERIFIED,
        "execution_armed": True,
        "phase": MarketPhase.ACTIVE,
        "window_triggered": True,
        "ptb": Decimal("64000.00"),
        "strategy_enabled": True,
        "strategy_id": "arc_twap_locked_buffer",
        "intent_exists": False,
        "quota_used": 0,
        "quota_reserved": 0,
        "max_trades_per_market": 3,
        "direction": Direction.UP,
        "directions_held": frozenset(),
        "allow_opposing_directions": False,
        "open_positions": 0,
        "max_concurrent_positions": 3,
        "limit_price": Decimal("0.70"),
        "size": Decimal("35"),
        "entry_price_min": Decimal("0.55"),
        "entry_price_max": Decimal("0.85"),
        "min_tradable_size": Decimal("5"),
        "daily_loss_usd": Decimal("0"),
        "consecutive_losses": 0,
        "max_daily_loss_usd": Decimal("50.00"),
        "max_consecutive_losses": 5,
        "feed_blocked": False,
        "feed_age_ms": 100.0,
        "clock_drift_critical": False,
        "clock_drift_ms": 12.0,
        "runtime_healthy": True,
    }
    base.update(overrides)
    return RiskContext(**base)  # type: ignore[arg-type]


@pytest.fixture
def engine() -> RiskEngine:
    return RiskEngine()


class TestTheBaselinePasses:
    def test_a_fully_healthy_context_is_allowed(self, engine: RiskEngine) -> None:
        """If this fails, every denial test below would pass for the wrong reason."""
        verdict = engine.evaluate(_context())
        assert verdict.allowed, (verdict.gate, verdict.detail)
        assert verdict.reason is None


class TestTheGateInventory:
    def test_there_are_exactly_fifteen_gates(self) -> None:
        assert len(GATE_ORDER) == GATE_COUNT
        assert len(set(GATE_ORDER)) == GATE_COUNT

    def test_every_declared_gate_exists_as_a_method(self, engine: RiskEngine) -> None:
        """Name-based dispatch means a rename without a GATE_ORDER update would raise
        on the first evaluation rather than silently never running."""
        for name in GATE_ORDER:
            assert callable(getattr(engine, f"_gate_{name}"))

    def test_no_gate_method_exists_outside_the_declared_order(self) -> None:
        """A gate that is implemented but not listed would never run, and its absence
        would be invisible."""
        implemented = {
            name.removeprefix("_gate_")
            for name, _ in inspect.getmembers(RiskEngine, inspect.isfunction)
            if name.startswith("_gate_")
        }
        assert implemented == set(GATE_ORDER)

    def test_no_lead_time_gate_exists(self) -> None:
        """D1: the lead-time gate is repealed ENTIRELY. The only execution boundary is
        the market phase."""
        repealed = ("lead", "too_late", "too_close", "deadline", "insufficient_time")
        for name in GATE_ORDER:
            assert not any(word in name for word in repealed), name


class TestTheContextIsFrozen:
    def test_a_gate_cannot_mutate_the_context(self) -> None:
        """A gate that could reach live state would evaluate a different world than
        the earlier gates saw, and the verdict would depend on how long it took."""
        context = _context()
        with pytest.raises(FrozenInstanceError):
            context.trading_enabled = False  # type: ignore[misc]


class TestGate1TradingEnabled:
    def test_an_unverified_spec_denies_every_intent(self, engine: RiskEngine) -> None:
        """A8. Enforced HERE because this is the single place an order can be
        authorised, so a caller that forgets the runtime flag still cannot submit."""
        verdict = engine.evaluate(_context(spec_status=SettlementSpecStatus.UNVERIFIED))
        assert verdict.reason is DenialReason.TRADING_DISABLED_SPEC_UNVERIFIED
        assert verdict.gate == "trading_enabled"

    def test_a_failed_spec_denies_too(self, engine: RiskEngine) -> None:
        verdict = engine.evaluate(_context(spec_status=SettlementSpecStatus.FAILED))
        assert verdict.reason is DenialReason.TRADING_DISABLED_SPEC_UNVERIFIED

    def test_a_paused_operator_gets_its_own_reason(self, engine: RiskEngine) -> None:
        verdict = engine.evaluate(_context(paused=True))
        assert verdict.reason is DenialReason.TRADING_PAUSED

    def test_disabled_trading_denies_even_with_a_verified_spec(
        self, engine: RiskEngine
    ) -> None:
        verdict = engine.evaluate(
            _context(trading_enabled=False, trading_disabled_reason="operator stopped")
        )
        assert verdict.denied
        assert verdict.detail == "operator stopped"

    def test_it_runs_before_every_other_gate(self, engine: RiskEngine) -> None:
        """So a market that is broken in five ways still reports the A8 boundary,
        which is the one the operator must act on."""
        verdict = engine.evaluate(
            _context(
                spec_status=SettlementSpecStatus.UNVERIFIED,
                phase=MarketPhase.DEAD,
                window_triggered=False,
                ptb=None,
            )
        )
        assert verdict.reason is DenialReason.TRADING_DISABLED_SPEC_UNVERIFIED


class TestGate2ExecutionArmed:
    def test_a_disarmed_runtime_denies_every_intent(self, engine: RiskEngine) -> None:
        """The operator gate. Default after every startup, so it must have its own
        reason: reporting it as an unverified spec would send the operator hunting a
        verification failure that never happened."""
        verdict = engine.evaluate(_context(execution_armed=False))
        assert verdict.reason is DenialReason.EXECUTION_NOT_ARMED
        assert verdict.gate == "execution_armed"

    def test_the_system_gate_wins_when_both_block(self, engine: RiskEngine) -> None:
        """An operator told "not armed" while the real obstacle is an unverified spec
        would arm the bot and see nothing happen, with no reason for the second
        refusal."""
        verdict = engine.evaluate(
            _context(execution_armed=False, spec_status=SettlementSpecStatus.UNVERIFIED)
        )
        assert verdict.gate == "trading_enabled"


class TestGate3MarketPhase:
    def test_cancelling_is_the_only_execution_boundary(self, engine: RiskEngine) -> None:
        verdict = engine.evaluate(_context(phase=MarketPhase.CANCELLING))
        assert verdict.reason is DenialReason.MARKET_CANCELLING

    def test_a_dead_market_is_never_traded(self, engine: RiskEngine) -> None:
        """A1 Rule 1: no official PTB, no trading this market."""
        verdict = engine.evaluate(_context(phase=MarketPhase.DEAD))
        assert verdict.reason is DenialReason.MARKET_DEAD

    @pytest.mark.parametrize(
        "phase", [MarketPhase.DISCOVERED, MarketPhase.SETTLING, MarketPhase.SETTLED]
    )
    def test_any_other_non_active_phase_is_denied(
        self, engine: RiskEngine, phase: MarketPhase
    ) -> None:
        verdict = engine.evaluate(_context(phase=phase))
        assert verdict.reason is DenialReason.MARKET_NOT_ACTIVE
        assert phase.value in verdict.detail


class TestGate4WindowTriggered:
    def test_an_untriggered_window_cannot_become_an_intent(self, engine: RiskEngine) -> None:
        """Defence in depth: without it a caller could trade on no signal at all
        while every log line still read normally."""
        verdict = engine.evaluate(_context(window_triggered=False))
        assert verdict.reason is DenialReason.WINDOW_NOT_TRIGGERED


class TestGate5PriceToBeat:
    def test_a_missing_ptb_denies(self, engine: RiskEngine) -> None:
        verdict = engine.evaluate(_context(ptb=None))
        assert verdict.reason is DenialReason.PTB_UNAVAILABLE

    @pytest.mark.parametrize("ptb", [Decimal("0"), Decimal("-1")])
    def test_a_non_positive_ptb_denies(self, engine: RiskEngine, ptb: Decimal) -> None:
        assert engine.evaluate(_context(ptb=ptb)).reason is DenialReason.PTB_UNAVAILABLE

    def test_it_is_separate_from_the_dead_phase_check(self, engine: RiskEngine) -> None:
        """A market can be ACTIVE with the PTB freeze still absent, and trading it
        would mean trading against a reference this process invented."""
        verdict = engine.evaluate(_context(phase=MarketPhase.ACTIVE, ptb=None))
        assert verdict.reason is DenialReason.PTB_UNAVAILABLE


class TestGate6StrategyEnabled:
    def test_a_disabled_strategy_says_so(self, engine: RiskEngine) -> None:
        """Without this, a registry that lost its strategy would produce five
        ordinary non-signals per market and nothing would report why."""
        verdict = engine.evaluate(_context(strategy_enabled=False))
        assert verdict.reason is DenialReason.STRATEGY_DISABLED
        assert "arc_twap_locked_buffer" in verdict.detail

    def test_a_missing_strategy_id_still_produces_a_readable_detail(
        self, engine: RiskEngine
    ) -> None:
        verdict = engine.evaluate(_context(strategy_enabled=False, strategy_id=""))
        assert "(none)" in verdict.detail


class TestGate7DuplicateIntent:
    def test_a_window_with_an_intent_is_denied(self, engine: RiskEngine) -> None:
        verdict = engine.evaluate(_context(intent_exists=True))
        assert verdict.reason is DenialReason.DUPLICATE_INTENT


class TestGate8TradeQuota:
    def test_used_plus_reserved_is_what_counts(self, engine: RiskEngine) -> None:
        """Hazard H2: three windows can each pass a used-only check inside one second,
        before any of them fills, and open four positions against a three-trade
        budget."""
        verdict = engine.evaluate(
            _context(quota_used=1, quota_reserved=2, max_trades_per_market=3)
        )
        assert verdict.reason is DenialReason.TRADE_QUOTA_EXHAUSTED
        assert "1 used + 2 reserved of 3" in verdict.detail

    def test_reservations_alone_can_exhaust_the_quota(self, engine: RiskEngine) -> None:
        verdict = engine.evaluate(
            _context(quota_used=0, quota_reserved=3, max_trades_per_market=3)
        )
        assert verdict.reason is DenialReason.TRADE_QUOTA_EXHAUSTED

    def test_the_last_available_slot_is_allowed(self, engine: RiskEngine) -> None:
        assert engine.evaluate(
            _context(quota_used=2, quota_reserved=0, max_trades_per_market=3)
        ).allowed


class TestGate9OpposingDirection:
    def test_the_opposite_side_being_held_blocks_by_default(self, engine: RiskEngine) -> None:
        """Hazard H3: UP at 0.79 plus DOWN at 0.22 costs 1.01 and returns exactly
        1.00. Not a hedge; a fee."""
        verdict = engine.evaluate(
            _context(direction=Direction.UP, directions_held=frozenset({Direction.DOWN}))
        )
        assert verdict.reason is DenialReason.OPPOSING_DIRECTION_BLOCKED

    def test_it_is_symmetric(self, engine: RiskEngine) -> None:
        verdict = engine.evaluate(
            _context(direction=Direction.DOWN, directions_held=frozenset({Direction.UP}))
        )
        assert verdict.reason is DenialReason.OPPOSING_DIRECTION_BLOCKED

    def test_the_same_direction_being_held_is_fine(self, engine: RiskEngine) -> None:
        assert engine.evaluate(
            _context(direction=Direction.UP, directions_held=frozenset({Direction.UP}))
        ).allowed

    def test_the_operator_can_deliberately_allow_it(self, engine: RiskEngine) -> None:
        assert engine.evaluate(
            _context(
                direction=Direction.UP,
                directions_held=frozenset({Direction.DOWN}),
                allow_opposing_directions=True,
            )
        ).allowed


class TestGate10PositionLimit:
    def test_the_limit_is_process_wide_not_per_market(self, engine: RiskEngine) -> None:
        """Two markets are live at every boundary (D6), so a per-market quota alone
        permits twice the intended exposure in exactly the seconds the late windows
        trade in."""
        verdict = engine.evaluate(_context(open_positions=3, max_concurrent_positions=3))
        assert verdict.reason is DenialReason.POSITION_LIMIT_REACHED
        assert "3 open of 3" in verdict.detail

    def test_one_slot_below_the_limit_is_allowed(self, engine: RiskEngine) -> None:
        assert engine.evaluate(
            _context(open_positions=2, max_concurrent_positions=3)
        ).allowed


class TestGate11EntryBand:
    def test_a_price_above_the_band_is_denied(self, engine: RiskEngine) -> None:
        verdict = engine.evaluate(_context(limit_price=Decimal("0.86")))
        assert verdict.reason is DenialReason.ENTRY_PRICE_LIMIT

    def test_a_price_below_the_band_has_its_own_reason(self, engine: RiskEngine) -> None:
        """Too expensive is a trade not worth taking; too cheap is a signal the book
        disagrees with. Opposite meanings, so separate reasons."""
        verdict = engine.evaluate(_context(limit_price=Decimal("0.54")))
        assert verdict.reason is DenialReason.ENTRY_PRICE_BELOW_MIN

    @pytest.mark.parametrize("price", [Decimal("0.55"), Decimal("0.85")])
    def test_both_boundaries_are_inclusive(self, engine: RiskEngine, price: Decimal) -> None:
        assert engine.evaluate(_context(limit_price=price)).allowed

    def test_an_already_floored_price_at_the_ceiling_is_admitted(
        self, engine: RiskEngine
    ) -> None:
        """D2: 0.857 floors to 0.85 and is admissible. Validating first and flooring
        second would reject a price the venue would have accepted."""
        assert engine.evaluate(_context(limit_price=Decimal("0.85"))).allowed


class TestGate12ExchangeMinimum:
    def test_a_sub_minimum_size_is_denied(self, engine: RiskEngine) -> None:
        verdict = engine.evaluate(_context(size=Decimal("4")))
        assert verdict.reason is DenialReason.SIZE_BELOW_EXCHANGE_MINIMUM

    def test_exactly_the_minimum_is_allowed(self, engine: RiskEngine) -> None:
        assert engine.evaluate(_context(size=Decimal("5"))).allowed

    @pytest.mark.parametrize("size", [Decimal("0"), Decimal("-1")])
    def test_a_non_positive_size_is_denied_even_with_no_minimum(
        self, engine: RiskEngine, size: Decimal
    ) -> None:
        verdict = engine.evaluate(_context(size=size, min_tradable_size=Decimal("0")))
        assert verdict.reason is DenialReason.SIZE_BELOW_EXCHANGE_MINIMUM


class TestGate13LossLimits:
    def test_the_daily_loss_limit_stops_trading(self, engine: RiskEngine) -> None:
        verdict = engine.evaluate(
            _context(daily_loss_usd=Decimal("50.00"), max_daily_loss_usd=Decimal("50.00"))
        )
        assert verdict.reason is DenialReason.LOSS_LIMIT_REACHED
        assert "daily loss" in verdict.detail

    def test_the_consecutive_loss_limit_stops_trading(self, engine: RiskEngine) -> None:
        verdict = engine.evaluate(_context(consecutive_losses=5, max_consecutive_losses=5))
        assert verdict.reason is DenialReason.LOSS_LIMIT_REACHED
        assert "consecutive losses" in verdict.detail

    def test_the_detail_says_which_threshold_fired(self, engine: RiskEngine) -> None:
        """One reason, because both mean "stop trading today". Two details, because a
        losing streak and one large loss are separate signals."""
        daily = engine.evaluate(
            _context(daily_loss_usd=Decimal("99"), max_daily_loss_usd=Decimal("50.00"))
        )
        streak = engine.evaluate(_context(consecutive_losses=9, max_consecutive_losses=5))
        assert daily.detail != streak.detail

    def test_zero_disables_the_daily_limit(self, engine: RiskEngine) -> None:
        assert engine.evaluate(
            _context(daily_loss_usd=Decimal("9999"), max_daily_loss_usd=Decimal("0"))
        ).allowed

    def test_zero_disables_the_consecutive_limit(self, engine: RiskEngine) -> None:
        assert engine.evaluate(
            _context(consecutive_losses=99, max_consecutive_losses=0)
        ).allowed

    def test_a_profitable_day_arrives_as_zero_not_as_a_negative(
        self, engine: RiskEngine
    ) -> None:
        """daily_loss_usd is a POSITIVE magnitude. A negative would compare as under
        every threshold by accident, which is right here but wrong the moment the
        comparison is ever inverted."""
        assert engine.evaluate(_context(daily_loss_usd=Decimal("0"))).allowed


class TestGate14FeedFreshness:
    def test_a_blocked_feed_denies(self, engine: RiskEngine) -> None:
        """The failure is the quiet one: socket up, process healthy, last observation
        forty seconds old, and the TWAP describes a market that has since moved."""
        verdict = engine.evaluate(_context(feed_blocked=True, feed_age_ms=40000.0))
        assert verdict.reason is DenialReason.FEED_STALE
        assert "40000ms ago" in verdict.detail

    def test_a_feed_that_never_delivered_reads_as_never(self, engine: RiskEngine) -> None:
        verdict = engine.evaluate(_context(feed_blocked=True, feed_age_ms=None))
        assert "never" in verdict.detail


class TestGate15RuntimeHealth:
    def test_critical_clock_drift_denies(self, engine: RiskEngine) -> None:
        """On a three-second window, drift near a one-second threshold consumes a
        third of the window."""
        verdict = engine.evaluate(_context(clock_drift_critical=True, clock_drift_ms=1200.0))
        assert verdict.reason is DenialReason.CLOCK_DRIFT_CRITICAL

    def test_negative_drift_is_reported_as_read(self, engine: RiskEngine) -> None:
        """Classification is on the ABSOLUTE offset; -1200ms is as bad as +1200ms."""
        verdict = engine.evaluate(_context(clock_drift_critical=True, clock_drift_ms=-1200.0))
        assert verdict.reason is DenialReason.CLOCK_DRIFT_CRITICAL
        assert "-1200ms" in verdict.detail

    def test_general_unhealthiness_denies_with_its_own_reason(
        self, engine: RiskEngine
    ) -> None:
        verdict = engine.evaluate(_context(runtime_healthy=False, runtime_detail="db locked"))
        assert verdict.reason is DenialReason.RUNTIME_UNHEALTHY
        assert verdict.detail == "db locked"

    def test_drift_is_reported_before_general_health(self, engine: RiskEngine) -> None:
        verdict = engine.evaluate(_context(clock_drift_critical=True, runtime_healthy=False))
        assert verdict.reason is DenialReason.CLOCK_DRIFT_CRITICAL


class TestOrderIsDeterministic:
    def test_each_gate_reports_itself_when_it_is_the_only_failure(
        self, engine: RiskEngine
    ) -> None:
        """Every gate is individually reachable. A gate fully shadowed by an earlier
        one would be dead code that reads as protection."""
        breakages: dict[str, dict[str, object]] = {
            "trading_enabled": {"spec_status": SettlementSpecStatus.UNVERIFIED},
            "execution_armed": {"execution_armed": False},
            "market_phase": {"phase": MarketPhase.CANCELLING},
            "window_triggered": {"window_triggered": False},
            "price_to_beat": {"ptb": None},
            "strategy_enabled": {"strategy_enabled": False},
            "duplicate_intent": {"intent_exists": True},
            "trade_quota": {"quota_used": 3},
            "opposing_direction": {"directions_held": frozenset({Direction.DOWN})},
            "position_limit": {"open_positions": 3},
            "entry_band": {"limit_price": Decimal("0.99")},
            "exchange_minimum": {"size": Decimal("1")},
            "loss_limits": {"consecutive_losses": 5},
            "feed_freshness": {"feed_blocked": True},
            "runtime_health": {"runtime_healthy": False},
        }
        assert set(breakages) == set(GATE_ORDER)
        for gate, breakage in breakages.items():
            verdict = engine.evaluate(_context(**breakage))
            assert verdict.gate == gate, (gate, verdict.gate, verdict.detail)

    def test_the_earliest_broken_gate_always_wins(self, engine: RiskEngine) -> None:
        """Short-circuit at the first denial, so the same situation always reports the
        same reason and a log change means a world change."""
        for index in range(len(GATE_ORDER)):
            broken: dict[str, object] = {
                "spec_status": SettlementSpecStatus.UNVERIFIED,
                "execution_armed": False,
                "phase": MarketPhase.CANCELLING,
                "window_triggered": False,
                "ptb": None,
                "strategy_enabled": False,
                "intent_exists": True,
                "quota_used": 99,
                "directions_held": frozenset({Direction.DOWN}),
                "open_positions": 99,
                "limit_price": Decimal("0.99"),
                "size": Decimal("0"),
                "consecutive_losses": 99,
                "feed_blocked": True,
                "runtime_healthy": False,
            }
            healthy_again: dict[str, object] = {
                "spec_status": SettlementSpecStatus.VERIFIED,
                "execution_armed": True,
                "phase": MarketPhase.ACTIVE,
                "window_triggered": True,
                "ptb": Decimal("64000"),
                "strategy_enabled": True,
                "intent_exists": False,
                "quota_used": 0,
                "directions_held": frozenset(),
                "open_positions": 0,
                "limit_price": Decimal("0.70"),
                "size": Decimal("35"),
                "consecutive_losses": 0,
                "feed_blocked": False,
                "runtime_healthy": True,
            }
            keys = list(healthy_again)
            # Repair everything before `index`, leave the rest broken.
            context_values = dict(broken)
            for key in keys[:index]:
                context_values[key] = healthy_again[key]
            verdict = engine.evaluate(_context(**context_values))
            assert verdict.gate == GATE_ORDER[index]

    def test_the_same_context_produces_the_same_verdict_every_time(
        self, engine: RiskEngine
    ) -> None:
        context = _context(quota_used=3)
        first = engine.evaluate(context)
        for _ in range(100):
            assert engine.evaluate(context) == first


class TestTheEngineIsPure:
    def test_it_has_no_attribute_storage(self) -> None:
        """A risk engine that remembered the previous market's quota would deny
        trades on a fresh market and the denial would look like a working limit."""
        assert RiskEngine.__slots__ == ()
        with pytest.raises(AttributeError):
            RiskEngine().cache = {}  # type: ignore[attr-defined]

    def test_the_source_imports_nothing_that_performs_io(self) -> None:
        tree = ast.parse(Path("arc/risk/engine.py").read_text(encoding="utf-8"))
        forbidden = {
            "time",
            "asyncio",
            "socket",
            "httpx",
            "sqlite3",
            "random",
            "arc.storage",
            "arc.storage.store",
            "arc.clock",
        }
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(a.name for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
        assert imported.isdisjoint(forbidden), imported & forbidden

    def test_the_source_names_no_repealed_lead_time_identifier(self) -> None:
        """D1 named five identifiers that must not exist anywhere."""
        source = Path("arc/risk/engine.py").read_text(encoding="utf-8")
        for name in (
            "min_execution_lead_ms",
            "INSUFFICIENT_LEAD_TIME",
            "last_intent_ts",
            "is_intent_admissible",
            "LeadTimeInvariantError",
        ):
            assert name not in source, name


class TestLimitsProjection:
    def test_every_configured_bound_reaches_the_risk_layer(self) -> None:
        trading = build_trading_config(dict(VALID_TRADING_VALUES))
        limits = limits_from_trading(trading)
        assert limits.max_trades_per_market == trading.max_trades_per_market
        assert limits.max_concurrent_positions == trading.max_concurrent_positions
        assert limits.max_daily_loss_usd == trading.max_daily_loss_usd
        assert limits.max_consecutive_losses == trading.max_consecutive_losses
        assert limits.entry_price_min == trading.entry_price_min
        assert limits.entry_price_max == trading.entry_price_max
        assert limits.min_tradable_size == trading.min_tradable_size
        assert limits.allow_opposing_directions == trading.allow_opposing_directions

    def test_the_strategy_cannot_see_a_risk_limit(self) -> None:
        """A strategy that could read the limits could shape its proposal to slip
        past a gate, and the gates would be measuring a decision made with the gates
        in mind."""
        from dataclasses import fields

        from arc.strategy.config import StrategyConfig

        names = {f.name for f in fields(StrategyConfig)}
        assert names.isdisjoint(
            {
                "max_trades_per_market",
                "max_concurrent_positions",
                "max_daily_loss_usd",
                "max_consecutive_losses",
                "allow_opposing_directions",
            }
        )
