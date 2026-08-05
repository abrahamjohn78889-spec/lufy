"""The decision layer's inputs and boundaries: snapshot, adapter, and what it cannot do.

The engine's behaviour is in test_decision_engine.py. This file is about the layer
itself — that it reads the frozen state once, hands the strategy only what the
strategy is allowed to see, and has no way to submit, cancel or time anything.
"""

from __future__ import annotations

import ast
from dataclasses import fields
from decimal import Decimal
from pathlib import Path

import pytest
from decision_fixtures import (
    BASE_PTB,
    DEFAULT_QUOTE,
    WINDOW_TS,
    fired_market,
    strategy_config,
)

from arc.decision.snapshot import snapshot_for
from arc.decision.strategy import context_for
from arc.domain.enums import Direction, MarketPhase, WindowState
from arc.domain.models import MarketInstance
from arc.strategy.protocol import StrategyContext

DECISION_PACKAGE = Path("arc/decision")

# Anything that would let a decision depend on when it ran, or reach the venue.
FORBIDDEN_IMPORTS = frozenset(
    {
        "time",
        "asyncio",
        "socket",
        "httpx",
        "websockets",
        "random",
        "datetime",
        "arc.clock",
        "arc.feed",
        "arc.market.discovery",
        "arc.market.ptb",
        "arc.runtime.watchdog",
    }
)

# Names that would mean this layer had started scheduling or submitting.
FORBIDDEN_CALLS = frozenset(
    {
        "monotonic",
        "sleep",
        "perf_counter",
        "call_later",
        "call_at",
        "create_task",
        "uuid4",
        "randint",
        "submit_order",
        "cancel_order",
        "place_order",
        "reprice",
    }
)


def _modules() -> list[Path]:
    return sorted(DECISION_PACKAGE.glob("*.py"))


def _trees() -> list[tuple[Path, ast.Module]]:
    return [(p, ast.parse(p.read_text(encoding="utf-8"))) for p in _modules()]


class TestTheSnapshotIsTakenOnce:
    def test_it_copies_the_frozen_values_out_of_the_window(self) -> None:
        market = fired_market()
        window = market.window(3)
        snapshot = snapshot_for(market, window)
        assert snapshot is not None
        assert snapshot.direction == window.direction
        assert snapshot.locked_trigger == window.locked_trigger
        assert snapshot.opening_twap == window.opening_twap
        assert snapshot.buffer == window.buffer
        assert snapshot.ptb == BASE_PTB

    def test_a_later_observation_does_not_change_a_taken_snapshot(self) -> None:
        """The failure this prevents: gate 3 reading one TWAP and the intent
        recording another, so the persisted intent states a number no gate saw."""
        market = fired_market()
        snapshot = snapshot_for(market, market.window(3))
        assert snapshot is not None
        before = snapshot.signal_twap
        market.accumulator.add(BASE_PTB + Decimal("500"))
        assert snapshot.signal_twap == before
        assert market.signal_twap != before

    def test_it_carries_the_markets_close_not_a_recomputed_one(self) -> None:
        market = fired_market()
        snapshot = snapshot_for(market, market.window(3))
        assert snapshot is not None
        assert snapshot.close_ts == market.close_ts

    def test_it_records_the_window_state_it_read(self) -> None:
        market = fired_market(fired=(3,))
        fired = snapshot_for(market, market.window(3))
        merely_frozen = snapshot_for(market, market.window(5))
        assert fired is not None and merely_frozen is not None
        assert fired.state is WindowState.FIRED
        assert merely_frozen.state is WindowState.FROZEN

    def test_there_is_no_snapshot_without_a_ptb(self) -> None:
        """A window frozen against no PTB has no reference to trade around, and a
        default of zero would make every UP direction correct.

        Built by hand rather than by unfreezing a fixture market, because freeze_ptb
        is one-way and there is no path in production that could undo it (A11).
        """
        market = MarketInstance.create(WINDOW_TS, (3,))
        market.phase = MarketPhase.ACTIVE
        market.accumulator.add(BASE_PTB)
        market.window(3).freeze(
            opening_twap=BASE_PTB,
            ptb=BASE_PTB,
            buffer=Decimal("1.00"),
            frozen_at=float(WINDOW_TS),
        )
        assert market.ptb is None
        assert snapshot_for(market, market.window(3)) is None

    def test_there_is_no_snapshot_without_an_observation(self) -> None:
        """No TWAP means nothing to compare with the trigger."""
        market = MarketInstance.create(WINDOW_TS, (3,))
        market.phase = MarketPhase.ACTIVE
        market.freeze_ptb(BASE_PTB)
        market.window(3).freeze(
            opening_twap=BASE_PTB,
            ptb=BASE_PTB,
            buffer=Decimal("1.00"),
            frozen_at=float(WINDOW_TS),
        )
        assert market.signal_twap is None
        assert snapshot_for(market, market.window(3)) is None

    def test_there_is_no_snapshot_for_an_expired_window(self) -> None:
        market = fired_market(offsets=(3,), fired=())
        market.window(3).mark_expired()
        assert market.window(3).state is WindowState.EXPIRED
        assert snapshot_for(market, market.window(3)) is None


class TestTheStrategyContextAdapter:
    def test_it_passes_the_frozen_five_verbatim(self) -> None:
        market = fired_market()
        snapshot = snapshot_for(market, market.window(3))
        assert snapshot is not None
        context = context_for(snapshot, strategy_config(), quote_price=DEFAULT_QUOTE)
        assert context.direction == snapshot.direction
        assert context.opening_twap == snapshot.opening_twap
        assert context.ptb == snapshot.ptb
        assert context.buffer == snapshot.buffer
        assert context.locked_trigger == snapshot.locked_trigger

    def test_it_passes_the_quote_the_caller_supplied(self) -> None:
        """The strategy performs no I/O, so it cannot fetch a book — and cannot
        therefore fetch one for the wrong side."""
        market = fired_market(direction=Direction.DOWN)
        snapshot = snapshot_for(market, market.window(3))
        assert snapshot is not None
        context = context_for(snapshot, strategy_config(), quote_price=Decimal("0.42"))
        assert context.quote_price == Decimal("0.42")
        assert context.direction is Direction.DOWN

    def test_it_passes_the_sizing_configuration(self) -> None:
        config = strategy_config()
        market = fired_market()
        snapshot = snapshot_for(market, market.window(3))
        assert snapshot is not None
        context = context_for(snapshot, config, quote_price=DEFAULT_QUOTE)
        assert context.position_notional_usd == config.position_notional_usd
        assert context.tick_size == config.tick_size
        assert context.min_tradable_size == config.min_tradable_size

    def test_it_passes_no_risk_limit(self) -> None:
        """A17: a strategy that could read the gates would shape its proposal to slip
        past them, and the gates would then be measuring a decision made with the
        gates in mind."""
        names = {f.name for f in fields(StrategyContext)}
        assert names.isdisjoint(
            {
                "max_concurrent_positions",
                "max_daily_loss_usd",
                "max_consecutive_losses",
                "max_trades_per_market",
                "allow_opposing_directions",
                "entry_price_min",
                "entry_price_max",
            }
        )

    def test_it_passes_no_settlement_twap(self) -> None:
        """A6: the venue's Chainlink TWAP is the OUTCOME quantity. A strategy able to
        read it would be fitting to the answer."""
        names = {f.name for f in fields(StrategyContext)}
        assert "settlement_twap" not in names
        assert "settlement" not in names

    def test_it_passes_no_handle_to_anything_mutable(self) -> None:
        market = fired_market()
        snapshot = snapshot_for(market, market.window(3))
        assert snapshot is not None
        context = context_for(snapshot, strategy_config(), quote_price=DEFAULT_QUOTE)
        for field in fields(StrategyContext):
            value = getattr(context, field.name)
            assert isinstance(value, str | int | Decimal | Direction), field.name


class TestTheLayerCannotReachOutside:
    """A0/A10 enforced structurally. An AST walk, not a grep, so a rename or an
    aliased import cannot slip past it."""

    def test_the_package_has_modules_to_check(self) -> None:
        """Guards the three tests below: an empty glob would pass all of them."""
        assert len(_modules()) >= 5

    def test_no_module_imports_a_clock_a_socket_or_a_venue(self) -> None:
        for path, tree in _trees():
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        assert alias.name not in FORBIDDEN_IMPORTS, f"{path}: {alias.name}"
                elif isinstance(node, ast.ImportFrom) and node.module:
                    assert node.module not in FORBIDDEN_IMPORTS, f"{path}: {node.module}"

    def test_no_module_calls_a_timer_or_an_order_api(self) -> None:
        for path, tree in _trees():
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                name = (
                    func.attr
                    if isinstance(func, ast.Attribute)
                    else func.id
                    if isinstance(func, ast.Name)
                    else ""
                )
                assert name not in FORBIDDEN_CALLS, f"{path}: {name}"

    def test_no_repealed_lead_time_identifier_survives(self) -> None:
        """D1 was repealed entirely. A surviving helper would be reachable and could
        be wired back in by a later edit that looked like a bug fix."""
        repealed = (
            "min_execution_lead_ms",
            "INSUFFICIENT_LEAD_TIME",
            "last_intent_ts",
            "is_intent_admissible",
            "LeadTimeInvariantError",
        )
        for path in _modules():
            source = path.read_text(encoding="utf-8")
            for name in repealed:
                assert name not in source, f"{path}: {name}"

    def test_the_only_clock_value_is_the_one_passed_in(self) -> None:
        """The engine records `now`; it never reads one. A parameter cannot be reached
        by the engine's own code path, so admissibility cannot depend on the time."""
        source = (DECISION_PACKAGE / "engine.py").read_text(encoding="utf-8")
        assert "import time" not in source
        assert "time.time()" not in source


class TestThePackageDeclaresItsSurface:
    @pytest.mark.parametrize("path", [str(p) for p in _modules()])
    def test_every_module_declares_all(self, path: str) -> None:
        tree = ast.parse(Path(path).read_text(encoding="utf-8"))
        names = {
            target.id
            for node in tree.body
            if isinstance(node, ast.Assign)
            for target in node.targets
            if isinstance(target, ast.Name)
        }
        assert "__all__" in names, path
