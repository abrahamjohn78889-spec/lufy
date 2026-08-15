"""MAJORITY end to end: entry trigger, fresh read, side, intent, order.

The unit tests in test_majority_core.py prove each piece in isolation. This file
proves the pieces compose — that an entry opportunity firing actually produces a
resting order with MAJORITY's identity on it, and that every path which must NOT
produce one does not.

Nothing internal is mocked. The real MajorityEngine, real Store, real Submitter,
real RiskEngine and real order FSM are used; the only substitute is the V1 paper
adapter, which is production code.

THE TWO-STEP RULE is the reason this file exists. The BTC-price entry trigger
decides WHEN the opportunity fires; the side comes from a FRESH CLOB read taken
after the fire. Every assertion about the side is written against a book that
CHANGED between the fire and the fresh read, because a test whose book is static
cannot tell a correct implementation from one that trades the trigger itself.
"""

from __future__ import annotations

import asyncio
import io
from collections.abc import Callable
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest
from conftest import OFFSETS, VALID_TRADING_VALUES
from execution_fixtures import bucket, make_market, store_at
from polymarket.models import AcceptedOrder, OrderBook, OrderId

from arc.clock import FrozenClock
from arc.config import ArcSettings, Settings, build_trading_config
from arc.domain.enums import Direction, MarketPhase, OrderState, SettlementSpecStatus
from arc.domain.health import RuntimeHealth
from arc.domain.models import ExecutionIntent, Fill, MarketInstance, Observation, Order
from arc.execution.fill_engine import FillEngine
from arc.execution.orders import new_order, transition
from arc.execution.reprice import MAX_PRICE_RETRIES, RepricePolicy, Repricer
from arc.execution.submit import Submitter
from arc.execution.v1_paper import PaperExecutor
from arc.execution.v2_live import LiveExecutor
from arc.majority.config import (
    MAJORITY_ENGINE,
    EntryMode,
    MajorityConfig,
    MajorityWindowConfig,
)
from arc.majority.engine import MajorityEngine
from arc.majority.identity import majority_intent_id_for
from arc.majority.state import MajorityMarketState, MajorityState
from arc.majority.trigger import BookSnapshot
from arc.market.discovery import build_discovery
from arc.market.feed import RtdsFeed
from arc.risk.engine import RiskVerdict
from arc.runtime.engine import ArcRuntime
from arc.runtime.ledger import ledger_records
from arc.runtime.state import RuntimeState
from arc.storage.store import Store

WINDOW = 30
PTB = Decimal("64000.00")
TICK = Decimal("0.01")


def config(
    *,
    enabled: bool = True,
    buffer: Decimal = Decimal("1.00"),
    trigger_price: Decimal = Decimal("0.90"),
    target_limit_price: Decimal = Decimal("0.85"),
    shares: Decimal = Decimal("20"),
    entry_price_min: Decimal = Decimal("0.05"),
    entry_price_max: Decimal = Decimal("0.99"),
    disable_reason: str = "",
    window: int = WINDOW,
    trigger_limit_enabled: bool = False,
    buffer_enabled: bool = True,
    price_retry_enabled: bool = False,
    price_retry_attempts: int = 5,
) -> MajorityConfig:
    """A tradable MAJORITY configuration. Each test varies one field.

    Defaults are the final spec §5 conservative posture with the buffer entry
    condition ON: most of this file exercises the buffer-triggered entry, which
    needs `buffer_enabled` and trades at the live best bid while the combined
    trigger/target switch stays OFF. Tests that need the trigger gate pass
    `trigger_limit_enabled=True` explicitly and must present a book at or above
    the trigger price BEFORE the trigger tick — the gate reads live books.
    """
    from arc.majority.config import MajorityWindowConfig

    return MajorityConfig(
        enabled=enabled,
        windows=(
            MajorityWindowConfig(
                execution_window_seconds=window,
                buffer=buffer,
                trigger_price=trigger_price,
                target_limit_price=target_limit_price,
                shares=shares,
                entry_price_min=entry_price_min,
                entry_price_max=entry_price_max,
                disable_reason=disable_reason,
            ),
        ),
        trigger_limit_enabled=trigger_limit_enabled,
        buffer_enabled=buffer_enabled,
        price_retry_enabled=price_retry_enabled,
        price_retry_attempts=price_retry_attempts,
    )


def healthy() -> RuntimeHealth:
    """Health with every gate satisfied, so a denial is caused by the test's subject.

    Spelled out rather than defaulted: the two fields that default to refusing
    (spec_status, execution_armed) are exactly the ones a test would otherwise trip
    over while believing it had proved something about the trigger.
    """
    return RuntimeHealth(
        trading_enabled=True,
        spec_status=SettlementSpecStatus.VERIFIED,
        execution_armed=True,
    )


def observe(market: MarketInstance, price: Decimal, ts: float) -> None:
    """Feed one BTC spot into the market, as the RTDS feed would."""
    market.add_observation(Observation(ts=ts, price=price))


def inside_window(market: MarketInstance, window: int = WINDOW) -> float:
    """A clock reading inside the execution window, one second past its opening."""
    return float(market.close_ts - window + 1)


def engine_at(
    tmp_path: Path,
    *,
    cfg: MajorityConfig | None = None,
    name: str = "arc.db",
) -> tuple[MajorityEngine, Store, PaperExecutor]:
    """A wired MAJORITY engine on its own store, with its own engine-scoped submitter.

    `minimum` is the smallest share count across the configured windows — mirrors
    the runtime's construction choice. A single-window config therefore picks up
    that window's share count directly.
    """
    settings = cfg if cfg is not None else config()
    store = store_at(tmp_path, name)
    executor = PaperExecutor()
    minimum = min(w.shares for w in settings.windows_by_offset) if settings.windows_by_offset else Decimal("1")
    submitter = Submitter(
        store,
        executor,
        bucket=bucket(),
        minimum=minimum,
        engine=MAJORITY_ENGINE,
    )
    engine = MajorityEngine(settings, store, executor, submitter, tick_size=TICK)
    return engine, store, executor


def live_market(store: Store, window_ts: int = 1754400000) -> MarketInstance:
    """An ACTIVE market with an official PTB frozen, as gate 5 requires."""
    market = make_market(store, window_ts)
    market.phase = MarketPhase.ACTIVE
    market.freeze_ptb(PTB)
    return market


def trigger_twap_support(
    engine: MajorityEngine,
    market: MarketInstance,
    now: float,
    *,
    spot: Decimal = Decimal("64001.00"),
) -> None:
    """Push the running TWAP across the buffer and tick once: the entry fires.

    One observation at `spot` makes the cumulative mean exactly `spot`, so with the
    default buffer of 1.00 against PTB 64000.00 the |TWAP - PTB| >= buffer entry
    condition is satisfied — the window-30 TWAP-support mode (spec §9).
    """
    observe(market, spot, now)
    asyncio.run(engine.tick(market, healthy(), now))


def trade_happy_path(
    tmp_path: Path, *, cfg: MajorityConfig | None = None
) -> tuple[MajorityEngine, Store, PaperExecutor, MarketInstance, float]:
    """Drive one market all the way to a resting MAJORITY order.

    Defaults to the combined trigger/target switch ON so the resting order rests
    at the configured target limit price. The book is quoted BEFORE the trigger
    tick because the trigger gate (final spec §10-§12) reads the live book and must
    see the trigger price reached before the buffer condition is evaluated.

    Returns everything a follow-up assertion could need: the engine, store,
    executor, market and the clock reading the trigger fired at.
    """
    settings = cfg if cfg is not None else config(trigger_limit_enabled=True)
    engine, store, executor = engine_at(tmp_path, cfg=settings)
    market = live_market(store)
    engine.open_market(market.slug, market.close_ts)
    now = inside_window(market)
    executor.quote(market.slug, Direction.UP, Decimal("0.95"))
    executor.quote(market.slug, Direction.DOWN, Decimal("0.20"))
    trigger_twap_support(engine, market, now)
    asyncio.run(engine.tick(market, healthy(), now + 0.2))
    return engine, store, executor, market, now


# ── A. the happy path ────────────────────────────────────────────────────────


class TestTheFullSequenceProducesOneRestingOrder:
    """Entry fire, fresh read, side lock, intent, order. Every step observed."""

    def test_a_fired_entry_reaches_a_resting_majority_order(self, tmp_path: Path) -> None:
        # Combined switch ON: the order rests at the configured target limit price,
        # and the trigger gate reads the live book before the buffer condition is
        # evaluated (final spec §10-§12) — so the book is quoted before the trigger
        # tick.
        engine, store, executor = engine_at(
            tmp_path, cfg=config(trigger_limit_enabled=True)
        )
        market = live_market(store)
        engine.open_market(market.slug, market.close_ts)
        now = inside_window(market)

        # The trigger gate needs the book at or above the trigger price to latch.
        executor.quote(market.slug, Direction.UP, Decimal("0.95"))
        executor.quote(market.slug, Direction.DOWN, Decimal("0.20"))

        # Step 1: the trigger gate latches, the TWAP-support entry condition fires.
        # No side is chosen yet.
        trigger_twap_support(engine, market, now)

        state = engine.state_for(market.slug)
        assert state is not None
        assert state.state is MajorityState.TRIGGERED
        assert state.selected_side is None, "the side must not be chosen on the trigger pass"
        assert state.entry_mode == EntryMode.TWAP_SUPPORT

        # Step 2: a second pass takes the fresh read, locks the side, submits.
        asyncio.run(engine.tick(market, healthy(), now + 0.2))

        state = engine.state_for(market.slug)
        assert state is not None
        assert state.selected_side is Direction.UP
        assert state.state is MajorityState.SUBMITTED

        intents = store.intents_for(market.slug, engine=MAJORITY_ENGINE)
        assert len(intents) == 1
        assert intents[0].intent_id == majority_intent_id_for(market.slug, WINDOW)
        assert intents[0].direction is Direction.UP
        assert intents[0].strategy_id == MAJORITY_ENGINE

        orders = store.orders_for(market.slug)
        assert len(orders) == 1
        assert orders[0].engine == MAJORITY_ENGINE
        assert orders[0].order_id.startswith(f"{MAJORITY_ENGINE}:")
        assert orders[0].size == Decimal("20")
        assert orders[0].price == Decimal("0.85"), "the configured target limit, not the book"

    def test_the_order_is_priced_from_config_not_from_the_book(self, tmp_path: Path) -> None:
        """The limit is MAJORITY's configured target. The book only decides the side.

        Asserted against a book nowhere near the configured limit, so a version
        that priced off the winning bid would produce 0.97 and fail here. The
        combined switch is ON so the target price governs; the book is quoted
        before the trigger tick so the trigger gate can latch (final spec §12).
        """
        engine, store, executor = engine_at(
            tmp_path, cfg=config(trigger_limit_enabled=True)
        )
        market = live_market(store)
        engine.open_market(market.slug, market.close_ts)
        now = inside_window(market)
        executor.quote(market.slug, Direction.UP, Decimal("0.97"))
        executor.quote(market.slug, Direction.DOWN, Decimal("0.02"))
        trigger_twap_support(engine, market, now)

        asyncio.run(engine.tick(market, healthy(), now + 0.2))

        orders = store.orders_for(market.slug)
        assert len(orders) == 1
        assert orders[0].price == Decimal("0.85")


# ── B. the two-step rule ─────────────────────────────────────────────────────


class TestTheSideComesFromTheFreshReadNotTheTrigger:
    """What pushed the BTC trigger is NOT necessarily the side bought.

    This is the single most important property in the module and the easiest to
    implement wrongly: the entry trigger says WHEN, the fresh read says WHICH.
    """

    def test_the_fresh_read_overrules_what_pushed_the_trigger(
        self, tmp_path: Path
    ) -> None:
        """BTC rose (trigger fired), but at the fresh read DOWN holds the majority.

        Spec H: the UP trigger fires first, MAJORITY then reads DOWN — the DOWN
        order is the correct outcome. The trigger never decides the side.
        """
        engine, store, executor = engine_at(tmp_path)
        market = live_market(store)
        engine.open_market(market.slug, market.close_ts)
        now = inside_window(market)

        trigger_twap_support(engine, market, now)  # BTC moved UP across the buffer

        state = engine.state_for(market.slug)
        assert state is not None
        assert state.state is MajorityState.TRIGGERED

        # The book at the fresh read disagrees with the trigger's push.
        executor.quote(market.slug, Direction.UP, Decimal("0.16"))
        executor.quote(market.slug, Direction.DOWN, Decimal("0.85"))
        asyncio.run(engine.tick(market, healthy(), now + 0.2))

        assert state.selected_side is Direction.DOWN, (
            "the side must come from the fresh read; BTC pushed the trigger up but "
            "DOWN held the majority when the side was determined"
        )
        intents = store.intents_for(market.slug, engine=MAJORITY_ENGINE)
        assert intents[0].direction is Direction.DOWN
        orders = store.orders_for(market.slug)
        assert orders[0].direction is Direction.DOWN

    def test_both_snapshots_are_kept_so_the_two_steps_are_auditable(
        self, tmp_path: Path
    ) -> None:
        """The trigger snapshot carries ENTRY evidence, the decision snapshot the book.

        Since Phase 1 the entry trigger is a BTC-price condition, so the trigger
        snapshot holds no book bids — the timing evidence lives on the state's
        entry fields, and the book evidence lives entirely on the decision
        snapshot. Both objects are kept, distinct, for audit.
        """
        engine, store, executor = engine_at(tmp_path)
        market = live_market(store)
        engine.open_market(market.slug, market.close_ts)
        now = inside_window(market)

        trigger_twap_support(engine, market, now)
        executor.quote(market.slug, Direction.UP, Decimal("0.10"))
        executor.quote(market.slug, Direction.DOWN, Decimal("0.93"))
        asyncio.run(engine.tick(market, healthy(), now + 0.2))

        state = engine.state_for(market.slug)
        assert state is not None
        assert state.trigger_snapshot is not None
        assert state.decision_snapshot is not None
        assert state.trigger_snapshot is not state.decision_snapshot
        # Entry evidence: a BTC condition, not a book read.
        assert state.trigger_snapshot.best_bid_up is None
        assert state.trigger_snapshot.best_bid_down is None
        assert state.entry_mode == EntryMode.TWAP_SUPPORT
        # TWAP-support entry has no BTC levels; the mode itself is the evidence.
        assert state.fired_level is None
        # Book evidence: the fresh read alone.
        assert state.decision_snapshot.best_bid_up == Decimal("0.10")
        assert state.decision_snapshot.best_bid_down == Decimal("0.93")


# ── the down side (spec B) ───────────────────────────────────────────────────


class TestTheDownSideIsTradedWhenMajoritySaysDown:
    def test_a_down_majority_produces_a_down_order_and_nothing_else(
        self, tmp_path: Path
    ) -> None:
        # Combined switch ON so the resting order carries the target limit price;
        # the book is quoted before the trigger tick for the trigger gate (§12).
        engine, store, executor = engine_at(
            tmp_path, cfg=config(trigger_limit_enabled=True)
        )
        market = live_market(store)
        engine.open_market(market.slug, market.close_ts)
        now = inside_window(market)

        executor.quote(market.slug, Direction.UP, Decimal("0.20"))
        executor.quote(market.slug, Direction.DOWN, Decimal("0.95"))
        # BTC fell across the buffer: the entry fires on a downward move too.
        trigger_twap_support(engine, market, now, spot=Decimal("63999.00"))
        asyncio.run(engine.tick(market, healthy(), now + 0.2))

        state = engine.state_for(market.slug)
        assert state is not None
        assert state.selected_side is Direction.DOWN
        intents = store.intents_for(market.slug, engine=MAJORITY_ENGINE)
        assert len(intents) == 1
        assert intents[0].direction is Direction.DOWN
        orders = store.orders_for(market.slug)
        assert len(orders) == 1
        assert orders[0].direction is Direction.DOWN
        assert orders[0].price == Decimal("0.85")


# ── BTC buffer triggers, window > 30s (spec D-I) ─────────────────────────────


class TestBtcBufferTriggersRememberTheirLevels:
    """Spec §6: capture the reference at window open, remember ref ± buffer,
    fire on the first crossing, and submit nothing from the trigger itself."""

    def test_reference_and_both_levels_are_captured_at_window_open(
        self, tmp_path: Path
    ) -> None:
        engine, store, _ = engine_at(tmp_path, cfg=config(window=60, buffer=Decimal("50")))
        market = live_market(store)
        engine.open_market(market.slug, market.close_ts)
        now = inside_window(market, window=60)
        observe(market, Decimal("64000.00"), now)

        asyncio.run(engine.tick(market, healthy(), now))

        state = engine.state_for(market.slug)
        assert state is not None
        assert state.entry_mode == EntryMode.BTC_TRIGGER
        assert state.btc_reference == Decimal("64000.00")
        assert state.btc_up_trigger == Decimal("64050.00")
        assert state.btc_down_trigger == Decimal("63950.00")
        assert state.state is MajorityState.WAITING_TRIGGER
        assert store.orders_for(market.slug) == ()

    def test_no_order_is_submitted_while_monitoring_between_levels(
        self, tmp_path: Path
    ) -> None:
        engine, store, _ = engine_at(tmp_path, cfg=config(window=60, buffer=Decimal("50")))
        market = live_market(store)
        engine.open_market(market.slug, market.close_ts)
        now = inside_window(market, window=60)
        observe(market, Decimal("64000.00"), now)
        asyncio.run(engine.tick(market, healthy(), now))

        # Spot wanders inside the band for several ticks: monitoring only.
        for step in range(1, 6):
            observe(market, Decimal("64020.00"), now + step * 0.2)
            asyncio.run(engine.tick(market, healthy(), now + step * 0.2))

        state = engine.state_for(market.slug)
        assert state is not None
        assert state.state is MajorityState.WAITING_TRIGGER
        assert not state.triggered
        assert store.orders_for(market.slug) == ()

    def test_the_up_level_fires_first_when_spot_crosses_up(
        self, tmp_path: Path
    ) -> None:
        engine, store, _ = engine_at(tmp_path, cfg=config(window=60, buffer=Decimal("50")))
        market = live_market(store)
        engine.open_market(market.slug, market.close_ts)
        now = inside_window(market, window=60)
        observe(market, Decimal("64000.00"), now)
        asyncio.run(engine.tick(market, healthy(), now))

        observe(market, Decimal("64060.00"), now + 0.5)
        asyncio.run(engine.tick(market, healthy(), now + 0.5))

        state = engine.state_for(market.slug)
        assert state is not None
        assert state.triggered
        assert state.fired_level == Decimal("64050.00")
        assert state.fired_spot == Decimal("64060.00")
        assert store.orders_for(market.slug) == (), "the trigger fires the sequence, not an order"

    def test_the_down_level_fires_when_spot_crosses_down(
        self, tmp_path: Path
    ) -> None:
        engine, store, _ = engine_at(tmp_path, cfg=config(window=60, buffer=Decimal("50")))
        market = live_market(store)
        engine.open_market(market.slug, market.close_ts)
        now = inside_window(market, window=60)
        observe(market, Decimal("64000.00"), now)
        asyncio.run(engine.tick(market, healthy(), now))

        observe(market, Decimal("63940.00"), now + 0.5)
        asyncio.run(engine.tick(market, healthy(), now + 0.5))

        state = engine.state_for(market.slug)
        assert state is not None
        assert state.triggered
        assert state.fired_level == Decimal("63950.00")
        assert state.fired_spot == Decimal("63940.00")

    def test_up_trigger_fires_but_majority_down_buys_down(
        self, tmp_path: Path
    ) -> None:
        """Spec H end to end: UP trigger first, MAJORITY DOWN, DOWN order."""
        engine, store, executor = engine_at(
            tmp_path, cfg=config(window=60, buffer=Decimal("50"))
        )
        market = live_market(store)
        engine.open_market(market.slug, market.close_ts)
        now = inside_window(market, window=60)
        observe(market, Decimal("64000.00"), now)
        asyncio.run(engine.tick(market, healthy(), now))
        observe(market, Decimal("64060.00"), now + 0.5)
        asyncio.run(engine.tick(market, healthy(), now + 0.5))

        executor.quote(market.slug, Direction.UP, Decimal("0.16"))
        executor.quote(market.slug, Direction.DOWN, Decimal("0.85"))
        asyncio.run(engine.tick(market, healthy(), now + 0.7))

        orders = store.orders_for(market.slug)
        assert len(orders) == 1
        assert orders[0].direction is Direction.DOWN
        assert orders[0].price == Decimal("0.85")

    def test_down_trigger_fires_but_majority_up_buys_up(
        self, tmp_path: Path
    ) -> None:
        """Spec I end to end: DOWN trigger first, MAJORITY UP, UP order."""
        engine, store, executor = engine_at(
            tmp_path, cfg=config(window=60, buffer=Decimal("50"))
        )
        market = live_market(store)
        engine.open_market(market.slug, market.close_ts)
        now = inside_window(market, window=60)
        observe(market, Decimal("64000.00"), now)
        asyncio.run(engine.tick(market, healthy(), now))
        observe(market, Decimal("63940.00"), now + 0.5)
        asyncio.run(engine.tick(market, healthy(), now + 0.5))

        executor.quote(market.slug, Direction.UP, Decimal("0.85"))
        executor.quote(market.slug, Direction.DOWN, Decimal("0.16"))
        asyncio.run(engine.tick(market, healthy(), now + 0.7))

        orders = store.orders_for(market.slug)
        assert len(orders) == 1
        assert orders[0].direction is Direction.UP
        assert orders[0].price == Decimal("0.85")


# ── zero buffer (spec J) ─────────────────────────────────────────────────────


class TestZeroBufferTradesDirectly:
    """Final spec §9: buffer ON at value 0. For a >30s window this is DIRECT —
    never wait for an artificial BTC + 0 / BTC - 0 movement; the window fires at
    open and the side comes from MAJORITY at a current valid market price. (The
    ≤30s zero-buffer case is TWAP_SUPPORT and is covered by the test matrix.)"""

    def test_zero_buffer_fires_at_window_open_with_no_btc_move(
        self, tmp_path: Path
    ) -> None:
        engine, store, executor = engine_at(
            tmp_path, cfg=config(window=45, buffer=Decimal("0"))
        )
        market = live_market(store)
        engine.open_market(market.slug, market.close_ts)
        now = inside_window(market, window=45)

        # No observations at all: DIRECT fires at window open with nothing to
        # wait for, and invents no BTC ± 0 movement.
        asyncio.run(engine.tick(market, healthy(), now))

        state = engine.state_for(market.slug)
        assert state is not None
        assert state.triggered
        assert state.entry_mode == EntryMode.DIRECT
        assert state.fired_level is None
        assert state.fired_spot is None

        executor.quote(market.slug, Direction.UP, Decimal("0.91"))
        executor.quote(market.slug, Direction.DOWN, Decimal("0.05"))
        asyncio.run(engine.tick(market, healthy(), now + 0.2))

        orders = store.orders_for(market.slug)
        assert len(orders) == 1
        assert orders[0].direction is Direction.UP


# ── the combined switch (spec M/N) ───────────────────────────────────────────


class TestTheCombinedTriggerAndLimitSwitch:
    """Spec §10: ONE switch for editable trigger + target limit price."""

    def test_switch_off_trades_at_the_currently_valid_market_price(
        self, tmp_path: Path
    ) -> None:
        engine, store, executor = engine_at(
            tmp_path, cfg=config(trigger_limit_enabled=False)
        )
        market = live_market(store)
        engine.open_market(market.slug, market.close_ts)
        now = inside_window(market)
        trigger_twap_support(engine, market, now)
        executor.quote(market.slug, Direction.UP, Decimal("0.91"))
        executor.quote(market.slug, Direction.DOWN, Decimal("0.05"))

        asyncio.run(engine.tick(market, healthy(), now + 0.2))

        orders = store.orders_for(market.slug)
        assert len(orders) == 1
        assert orders[0].price == Decimal("0.91"), (
            "switch OFF: the MAJORITY side's live best bid, not the configured target"
        )

    def test_switch_on_submits_at_the_configured_target_limit_price(
        self, tmp_path: Path
    ) -> None:
        engine, store, executor = engine_at(
            tmp_path, cfg=config(trigger_limit_enabled=True)
        )
        market = live_market(store)
        engine.open_market(market.slug, market.close_ts)
        now = inside_window(market)
        # Quote the book first: the trigger gate reads the live book and must see
        # the trigger price reached (0.97 >= 0.90) before the buffer condition is
        # evaluated (final spec §12).
        executor.quote(market.slug, Direction.UP, Decimal("0.97"))
        executor.quote(market.slug, Direction.DOWN, Decimal("0.02"))
        trigger_twap_support(engine, market, now)

        asyncio.run(engine.tick(market, healthy(), now + 0.2))

        orders = store.orders_for(market.slug)
        assert len(orders) == 1
        assert orders[0].price == Decimal("0.85")


# ── C. the final direction gate (spec §13) ───────────────────────────────────


def gate_setup(
    tmp_path: Path,
) -> tuple[MajorityEngine, Store, MarketInstance, MajorityMarketState, MajorityWindowConfig, float]:
    """An engine driven to SIDE_SELECTED without submitting.

    The fire and the fresh read happen through the public tick and the engine's
    determination step; submission is NOT, so the gate's duplicate checks start
    clean and each refusal can be exercised in isolation.
    """
    engine, store, executor = engine_at(tmp_path)
    market = live_market(store)
    engine.open_market(market.slug, market.close_ts)
    now = inside_window(market)
    trigger_twap_support(engine, market, now)
    executor.quote(market.slug, Direction.UP, Decimal("0.95"))
    executor.quote(market.slug, Direction.DOWN, Decimal("0.20"))
    state = engine.state_for(market.slug)
    assert state is not None
    window = engine.config.window_for(WINDOW)
    assert window is not None
    asyncio.run(engine._fresh_read_and_determine(market, state, window, now + 0.2))
    assert state.state is MajorityState.SIDE_SELECTED
    assert state.selected_side is Direction.UP
    return engine, store, market, state, window, now + 0.2


def gate_intent(
    market: MarketInstance,
    state: MajorityMarketState,
    window: MajorityWindowConfig,
    now: float,
    **overrides: object,
) -> ExecutionIntent:
    """A hand-built intent matching the locked side, with one knob per test."""
    fields = {
        "market_slug": market.slug,
        "offset_seconds": window.execution_window_seconds,
        "direction": state.selected_side,
        "signal_twap": Decimal("0"),
        "locked_trigger": Decimal("0"),
        "created_at": now,
        "intent_id": majority_intent_id_for(market.slug, window.execution_window_seconds),
        "ptb": PTB,
        "buffer": window.buffer,
        "limit_price": Decimal("0.85"),
        "size": Decimal("20"),
        "strategy_id": MAJORITY_ENGINE,
        "close_ts": market.close_ts,
    }
    fields.update(overrides)
    return ExecutionIntent(**fields)  # type: ignore[arg-type]


ALLOWED = RiskVerdict(allowed=True)


class TestTheFinalDirectionGate:
    """Spec §13: the mandatory pre-submission verification. Every failed check
    refuses the submission and names the check that refused. Never auto-correct."""

    def test_a_consistent_submission_passes_all_checks(self, tmp_path: Path) -> None:
        engine, store, market, state, window, now = gate_setup(tmp_path)
        intent = gate_intent(market, state, window, now)
        assert engine._gate_direction(market, state, window, intent, ALLOWED, (market.slug, WINDOW)) is None
        assert store.orders_for(market.slug) == (), "the gate verifies; it does not submit"

    def test_a_wrong_market_id_refuses(self, tmp_path: Path) -> None:
        engine, _, market, state, window, now = gate_setup(tmp_path)
        intent = gate_intent(market, state, window, now, market_slug="some-other-market")
        failure = engine._gate_direction(market, state, window, intent, ALLOWED, (market.slug, WINDOW))
        assert failure == "market id mismatch"

    def test_a_wrong_window_id_refuses(self, tmp_path: Path) -> None:
        engine, _, market, state, window, now = gate_setup(tmp_path)
        intent = gate_intent(market, state, window, now, offset_seconds=WINDOW + 15)
        failure = engine._gate_direction(market, state, window, intent, ALLOWED, (market.slug, WINDOW))
        assert failure == "window id mismatch"

    def test_a_state_from_another_window_refuses(self, tmp_path: Path) -> None:
        engine, _, market, state, window, now = gate_setup(tmp_path)
        intent = gate_intent(market, state, window, now)
        state.execution_window_seconds = WINDOW + 15
        failure = engine._gate_direction(market, state, window, intent, ALLOWED, (market.slug, WINDOW))
        assert failure == "state window mismatch"

    def test_a_non_majority_engine_identity_refuses(self, tmp_path: Path) -> None:
        engine, _, market, state, window, now = gate_setup(tmp_path)
        intent = gate_intent(market, state, window, now, strategy_id="TWAP")
        failure = engine._gate_direction(market, state, window, intent, ALLOWED, (market.slug, WINDOW))
        assert failure == "engine identity mismatch"

    def test_a_missing_majority_decision_refuses(self, tmp_path: Path) -> None:
        engine, _, market, state, window, now = gate_setup(tmp_path)
        intent = gate_intent(market, state, window, now)
        state.verdict = None
        failure = engine._gate_direction(market, state, window, intent, ALLOWED, (market.slug, WINDOW))
        assert failure == "no MAJORITY decision on record"

    def test_a_missing_locked_direction_refuses(self, tmp_path: Path) -> None:
        engine, _, market, state, window, now = gate_setup(tmp_path)
        state._selected_side = None
        intent = gate_intent(market, state, window, now, direction=Direction.UP)
        failure = engine._gate_direction(market, state, window, intent, ALLOWED, (market.slug, WINDOW))
        assert failure == "no locked direction"

    def test_an_order_direction_other_than_the_locked_side_refuses(
        self, tmp_path: Path
    ) -> None:
        """Spec C: a wrong-direction order is never submitted and never corrected."""
        engine, _, market, state, window, now = gate_setup(tmp_path)
        intent = gate_intent(market, state, window, now, direction=Direction.DOWN)
        failure = engine._gate_direction(market, state, window, intent, ALLOWED, (market.slug, WINDOW))
        assert failure == "order direction != locked MAJORITY direction"

    def test_a_decision_book_without_a_usable_bid_refuses(self, tmp_path: Path) -> None:
        engine, _, market, state, window, now = gate_setup(tmp_path)
        intent = gate_intent(market, state, window, now)
        state.decision_snapshot = BookSnapshot(
            best_bid_up=None, best_bid_down=None, read_at=now, fresh=True
        )
        failure = engine._gate_direction(market, state, window, intent, ALLOWED, (market.slug, WINDOW))
        assert failure == "decision book has no usable bid for the chosen side"

    def test_a_non_positive_price_refuses(self, tmp_path: Path) -> None:
        engine, _, market, state, window, now = gate_setup(tmp_path)
        intent = gate_intent(market, state, window, now, limit_price=Decimal("0"))
        failure = engine._gate_direction(market, state, window, intent, ALLOWED, (market.slug, WINDOW))
        assert failure == "price not positive"

    def test_an_off_tick_price_refuses(self, tmp_path: Path) -> None:
        engine, _, market, state, window, now = gate_setup(tmp_path)
        intent = gate_intent(market, state, window, now, limit_price=Decimal("0.855"))
        failure = engine._gate_direction(market, state, window, intent, ALLOWED, (market.slug, WINDOW))
        assert failure == "price not on the venue tick grid"

    def test_an_invalid_quantity_refuses(self, tmp_path: Path) -> None:
        engine, _, market, state, window, now = gate_setup(tmp_path)
        intent = gate_intent(market, state, window, now, size=Decimal("21"))
        failure = engine._gate_direction(market, state, window, intent, ALLOWED, (market.slug, WINDOW))
        assert failure == "quantity invalid"

    def test_a_risk_denial_refuses_with_the_gate_named(self, tmp_path: Path) -> None:
        engine, _, market, state, window, now = gate_setup(tmp_path)
        intent = gate_intent(market, state, window, now)
        denied = RiskVerdict(allowed=False, gate="entry_band", detail="limit above band")
        failure = engine._gate_direction(market, state, window, intent, denied, (market.slug, WINDOW))
        assert failure is not None
        assert failure.startswith("risk denied:")
        assert "G11" in failure

    def test_a_stale_decision_read_refuses(self, tmp_path: Path) -> None:
        engine, _, market, state, window, now = gate_setup(tmp_path)
        intent = gate_intent(market, state, window, now)
        state.decision_snapshot = BookSnapshot(
            best_bid_up=Decimal("0.95"),
            best_bid_down=Decimal("0.20"),
            read_at=now - 10.0,
            fresh=False,
        )
        failure = engine._gate_direction(market, state, window, intent, ALLOWED, (market.slug, WINDOW))
        assert failure == "decision read is stale"

    def test_a_second_submission_in_the_same_run_refuses(self, tmp_path: Path) -> None:
        engine, _, market, state, window, now = gate_setup(tmp_path)
        intent = gate_intent(market, state, window, now)
        engine._submitted.add((market.slug, WINDOW))
        failure = engine._gate_direction(market, state, window, intent, ALLOWED, (market.slug, WINDOW))
        assert failure == "duplicate submission in this run"

    def test_an_already_persisted_intent_refuses(self, tmp_path: Path) -> None:
        engine, store, market, state, window, now = gate_setup(tmp_path)
        intent = gate_intent(market, state, window, now)
        assert store.save_intent(intent, engine=MAJORITY_ENGINE) is True
        failure = engine._gate_direction(market, state, window, intent, ALLOWED, (market.slug, WINDOW))
        assert failure == "duplicate intent already persisted"


# ── no trade ──────────────────────────────────────────────────────────────────


class TestEveryNonTradePathSubmitsNothing:
    """INDETERMINATE, an unreached trigger, OFF, and a closed window."""

    def test_equal_bids_resolve_to_no_trade_and_are_never_tie_broken(
        self, tmp_path: Path
    ) -> None:
        engine, store, executor = engine_at(tmp_path)
        market = live_market(store)
        engine.open_market(market.slug, market.close_ts)
        now = inside_window(market)

        trigger_twap_support(engine, market, now)
        # Dead level on the fresh read. There is no majority to read off this book.
        executor.quote(market.slug, Direction.UP, Decimal("0.95"))
        executor.quote(market.slug, Direction.DOWN, Decimal("0.95"))
        asyncio.run(engine.tick(market, healthy(), now + 0.2))

        state = engine.state_for(market.slug)
        assert state is not None
        assert state.state is MajorityState.NO_TRADE
        assert state.selected_side is None
        assert store.intents_for(market.slug, engine=MAJORITY_ENGINE) == ()
        assert store.orders_for(market.slug) == ()

    def test_a_missing_side_on_the_fresh_read_is_no_trade(self, tmp_path: Path) -> None:
        """One side unreadable is INDETERMINATE, not a walkover for the other."""
        engine, store, executor = engine_at(tmp_path)
        market = live_market(store)
        engine.open_market(market.slug, market.close_ts)
        now = inside_window(market)

        trigger_twap_support(engine, market, now)
        executor.forget(market.slug)
        asyncio.run(engine.tick(market, healthy(), now + 0.2))

        state = engine.state_for(market.slug)
        assert state is not None
        assert state.state is MajorityState.NO_TRADE
        assert store.orders_for(market.slug) == ()

    def test_a_stale_fresh_read_is_no_trade(self, tmp_path: Path) -> None:
        """A read taken more than the freshness budget after the trigger is refused.

        The side must be chosen at the trigger instant. A read seconds later
        describes a different book, and buying from it is buying a majority that
        may no longer exist.
        """
        engine, store, executor = engine_at(tmp_path)
        market = live_market(store)
        engine.open_market(market.slug, market.close_ts)
        now = inside_window(market)

        trigger_twap_support(engine, market, now)
        executor.quote(market.slug, Direction.UP, Decimal("0.94"))
        executor.quote(market.slug, Direction.DOWN, Decimal("0.30"))
        # Ten seconds later: well past the 2.0s budget.
        asyncio.run(engine.tick(market, healthy(), now + 10.0))

        state = engine.state_for(market.slug)
        assert state is not None
        assert state.state is MajorityState.NO_TRADE
        assert store.orders_for(market.slug) == ()

    def test_a_twap_inside_the_buffer_keeps_waiting(self, tmp_path: Path) -> None:
        """|TWAP - PTB| below the buffer: monitoring, no fire, no order."""
        engine, store, _ = engine_at(tmp_path)
        market = live_market(store)
        engine.open_market(market.slug, market.close_ts)
        now = inside_window(market)

        observe(market, Decimal("64000.50"), now)
        for step in range(5):
            asyncio.run(engine.tick(market, healthy(), now + step * 0.2))

        state = engine.state_for(market.slug)
        assert state is not None
        assert state.state is MajorityState.WAITING_TRIGGER
        assert not state.triggered
        assert store.orders_for(market.slug) == ()

    def test_the_trigger_is_inclusive_at_exactly_the_threshold(self, tmp_path: Path) -> None:
        """`>=`, not `>`. A TWAP exactly one buffer from PTB fires the entry."""
        engine, store, _ = engine_at(tmp_path)
        market = live_market(store)
        engine.open_market(market.slug, market.close_ts)
        now = inside_window(market)

        observe(market, Decimal("64001.00"), now)
        asyncio.run(engine.tick(market, healthy(), now))

        state = engine.state_for(market.slug)
        assert state is not None
        assert state.triggered

    def test_nothing_happens_before_the_window_opens(self, tmp_path: Path) -> None:
        """A BTC move 60 seconds early is not traded 60 seconds early."""
        engine, store, _ = engine_at(tmp_path)
        market = live_market(store)
        engine.open_market(market.slug, market.close_ts)
        early = float(market.close_ts - WINDOW - 60)
        observe(market, Decimal("64010.00"), early)

        asyncio.run(engine.tick(market, healthy(), early))

        state = engine.state_for(market.slug)
        assert state is not None
        assert state.state is MajorityState.WAITING_WINDOW
        assert not state.triggered
        assert store.orders_for(market.slug) == ()

    def test_a_disabled_engine_reads_no_book_at_all(self, tmp_path: Path) -> None:
        """OFF means OFF: no state row, no trigger evaluation, no order."""
        engine, store, _ = engine_at(tmp_path, cfg=config(enabled=False))
        market = live_market(store)
        engine.open_market(market.slug, market.close_ts)
        observe(market, Decimal("64010.00"), inside_window(market))

        asyncio.run(engine.tick(market, healthy(), inside_window(market)))

        state = engine.state_for(market.slug)
        assert state is not None, "an OFF engine still reports a state, honestly OFF"
        assert state.state is MajorityState.OFF
        assert store.orders_for(market.slug) == ()

    def test_a_fail_closed_engine_submits_nothing(self, tmp_path: Path) -> None:
        """`disable_reason` overrides `enabled`. Both are checked, not one."""
        engine, store, _ = engine_at(
            tmp_path, cfg=config(disable_reason="45s window has no defined formula")
        )
        market = live_market(store)
        engine.open_market(market.slug, market.close_ts)
        observe(market, Decimal("64010.00"), inside_window(market))

        asyncio.run(engine.tick(market, healthy(), inside_window(market)))

        assert store.orders_for(market.slug) == ()


# ── the gates ─────────────────────────────────────────────────────────────────


class TestTheRiskGatesStillApplyToMajority:
    """MAJORITY chooses its own side; it does not choose whether it may trade."""

    def test_missing_ptb_does_not_block_trading(self, tmp_path: Path) -> None:
        """PTB is display-only — a market without a frozen PTB still trades.

        Gate 5 was intentionally de-gated per operator directive: PTB remains
        available for visual reference but never blocks order submission. A zero
        buffer on a >30s window runs DIRECT entry mode, which fires without
        consulting PTB and reaches the risk gates; gate 5 now always allows.
        """
        engine, store, executor = engine_at(
            tmp_path, cfg=config(window=45, buffer=Decimal("0"))
        )
        market = make_market(store)
        market.phase = MarketPhase.ACTIVE  # deliberately NOT frozen
        engine.open_market(market.slug, market.close_ts)
        now = inside_window(market, window=45)
        executor.quote(market.slug, Direction.UP, Decimal("0.95"))
        executor.quote(market.slug, Direction.DOWN, Decimal("0.20"))

        asyncio.run(engine.tick(market, healthy(), now))
        asyncio.run(engine.tick(market, healthy(), now + 0.2))

        state = engine.state_for(market.slug)
        assert state is not None
        assert state.selected_side is Direction.UP, "the side is determined"
        # PTB no longer gates — the order should be submitted even without one.
        assert state.state is MajorityState.SUBMITTED
        assert len(store.orders_for(market.slug)) >= 1

    def test_an_unarmed_operator_switch_denies(self, tmp_path: Path) -> None:
        engine, store, executor = engine_at(tmp_path)
        market = live_market(store)
        engine.open_market(market.slug, market.close_ts)
        now = inside_window(market)
        trigger_twap_support(engine, market, now)
        executor.quote(market.slug, Direction.UP, Decimal("0.95"))
        executor.quote(market.slug, Direction.DOWN, Decimal("0.20"))
        unarmed = RuntimeHealth(
            trading_enabled=True,
            spec_status=SettlementSpecStatus.VERIFIED,
            execution_armed=False,
        )

        asyncio.run(engine.tick(market, unarmed, now + 0.2))

        state = engine.state_for(market.slug)
        assert state is not None
        assert state.state is MajorityState.NO_TRADE
        assert "G02" in state.no_trade_reason
        assert store.orders_for(market.slug) == ()

    def test_a_limit_outside_the_entry_band_denies(self, tmp_path: Path) -> None:
        """MAJORITY's own band, not TWAP's.

        Combined switch ON so the configured target limit price (0.85) is the price
        the risk gates see — the entry band (max 0.50) then denies it at G11. With
        the switch OFF the live best bid would be refused by the pricing step before
        the band gate, which is a different (also correct) refusal.
        """
        engine, store, executor = engine_at(
            tmp_path,
            cfg=config(
                trigger_limit_enabled=True,
                target_limit_price=Decimal("0.85"),
                entry_price_max=Decimal("0.50"),
            ),
        )
        market = live_market(store)
        engine.open_market(market.slug, market.close_ts)
        now = inside_window(market)
        # Quote first so the trigger gate can latch (0.95 >= 0.90).
        executor.quote(market.slug, Direction.UP, Decimal("0.95"))
        executor.quote(market.slug, Direction.DOWN, Decimal("0.20"))
        trigger_twap_support(engine, market, now)

        asyncio.run(engine.tick(market, healthy(), now + 0.2))

        state = engine.state_for(market.slug)
        assert state is not None
        assert state.state is MajorityState.NO_TRADE
        assert "G11" in state.no_trade_reason
        assert store.orders_for(market.slug) == ()

    def test_a_cancelling_market_denies_on_phase(self, tmp_path: Path) -> None:
        engine, store, executor = engine_at(tmp_path)
        market = live_market(store)
        engine.open_market(market.slug, market.close_ts)
        now = inside_window(market)
        trigger_twap_support(engine, market, now)
        executor.quote(market.slug, Direction.UP, Decimal("0.95"))
        executor.quote(market.slug, Direction.DOWN, Decimal("0.20"))
        market.phase = MarketPhase.CANCELLING

        asyncio.run(engine.tick(market, healthy(), now + 0.2))

        assert store.orders_for(market.slug) == ()

    def test_a_down_majority_with_risk_block_places_no_order(
        self, tmp_path: Path
    ) -> None:
        """Final spec Part 30: MAJORITY DOWN + risk block → no order. The side is
        locked DOWN from the fresh read, but an unarmed execution switch denies the
        submission at G02 — the bot refuses the opportunity, it does not stop."""
        engine, store, executor = engine_at(tmp_path)
        market = live_market(store)
        engine.open_market(market.slug, market.close_ts)
        now = inside_window(market)
        trigger_twap_support(engine, market, now)
        executor.quote(market.slug, Direction.UP, Decimal("0.20"))
        executor.quote(market.slug, Direction.DOWN, Decimal("0.95"))
        unarmed = RuntimeHealth(
            trading_enabled=True,
            spec_status=SettlementSpecStatus.VERIFIED,
            execution_armed=False,
        )

        # One tick: the side locks DOWN from the fresh read, then the risk gates
        # deny the DOWN submission before anything reaches the venue.
        asyncio.run(engine.tick(market, unarmed, now + 0.2))

        state = engine.state_for(market.slug)
        assert state is not None
        assert state.selected_side is Direction.DOWN
        assert state.state is MajorityState.NO_TRADE
        assert "G02" in state.no_trade_reason
        assert store.orders_for(market.slug) == ()


# ── once per market ───────────────────────────────────────────────────────────


class TestMajorityTradesEachMarketAtMostOnce:
    """One trigger, one side, one order — however many passes the loop makes."""

    def test_repeated_ticks_after_submission_add_no_second_order(
        self, tmp_path: Path
    ) -> None:
        engine, store, executor = engine_at(tmp_path)
        market = live_market(store)
        engine.open_market(market.slug, market.close_ts)
        now = inside_window(market)
        trigger_twap_support(engine, market, now)
        executor.quote(market.slug, Direction.UP, Decimal("0.95"))
        executor.quote(market.slug, Direction.DOWN, Decimal("0.20"))

        asyncio.run(engine.tick(market, healthy(), now + 0.2))
        for step in range(2, 26):
            asyncio.run(engine.tick(market, healthy(), now + step * 0.2))

        assert len(store.intents_for(market.slug, engine=MAJORITY_ENGINE)) == 1
        assert len(store.orders_for(market.slug)) == 1

    def test_a_terminal_state_is_never_revisited(self, tmp_path: Path) -> None:
        """A NO_TRADE market does not start over when the book recovers.

        The trigger fires once per market. A book that goes level, then favours UP
        thirty seconds later, describes a second opportunity MAJORITY does not take.
        """
        engine, store, executor = engine_at(tmp_path)
        market = live_market(store)
        engine.open_market(market.slug, market.close_ts)
        now = inside_window(market)

        trigger_twap_support(engine, market, now)
        executor.quote(market.slug, Direction.UP, Decimal("0.95"))
        executor.quote(market.slug, Direction.DOWN, Decimal("0.95"))
        asyncio.run(engine.tick(market, healthy(), now + 0.2))
        state = engine.state_for(market.slug)
        assert state is not None
        assert state.state is MajorityState.NO_TRADE

        executor.quote(market.slug, Direction.DOWN, Decimal("0.04"))
        for step in range(2, 12):
            asyncio.run(engine.tick(market, healthy(), now + step * 0.2))

        assert state.state is MajorityState.NO_TRADE
        assert store.orders_for(market.slug) == ()


# ── per-market isolation ──────────────────────────────────────────────────────


class TestTwoMarketsDoNotShareMajorityState:
    """Per-market state objects, created fresh and thrown away (A11)."""

    def test_a_second_market_starts_from_zero(self, tmp_path: Path) -> None:
        engine, store, executor = engine_at(tmp_path)
        first = live_market(store, 1754400000)
        second = live_market(store, 1754400300)
        engine.open_market(first.slug, first.close_ts)
        engine.open_market(second.slug, second.close_ts)

        now = inside_window(first)
        trigger_twap_support(engine, first, now)
        executor.quote(first.slug, Direction.UP, Decimal("0.95"))
        executor.quote(first.slug, Direction.DOWN, Decimal("0.20"))
        asyncio.run(engine.tick(first, healthy(), now + 0.2))

        first_state = engine.state_for(first.slug)
        second_state = engine.state_for(second.slug)
        assert first_state is not None
        assert second_state is not None
        assert first_state.state is MajorityState.SUBMITTED
        assert first_state.selected_side is Direction.UP
        assert second_state.state is MajorityState.WAITING_WINDOW
        assert second_state.selected_side is None
        assert store.orders_for(second.slug) == ()

    def test_dropping_a_market_removes_its_state_entirely(self, tmp_path: Path) -> None:
        """Thrown away, never reset. There is no path that clears and reuses."""
        engine, store, _ = engine_at(tmp_path)
        market = live_market(store)
        engine.open_market(market.slug, market.close_ts)
        assert engine.state_for(market.slug) is not None

        engine.drop_market(market.slug)

        assert engine.state_for(market.slug) is None

    def test_dropping_a_market_never_opened_is_harmless(self, tmp_path: Path) -> None:
        engine, _, _ = engine_at(tmp_path)
        engine.drop_market("btc-updown-5m-1786070100")  # no exception

    def test_a_market_with_no_state_is_skipped_not_invented(self, tmp_path: Path) -> None:
        """`tick` refuses a market it was never told about, rather than inventing one."""
        engine, store, _ = engine_at(tmp_path)
        market = live_market(store)

        asyncio.run(engine.tick(market, healthy(), inside_window(market)))

        assert engine.state_for(market.slug) is None
        assert store.orders_for(market.slug) == ()


# ── restart ───────────────────────────────────────────────────────────────────


class TestRestartDoesNotDuplicateTheTrade:
    """A new process on the same database resumes rather than re-trades.

    The state dict is in memory and a restart loses it, so the guarantee cannot
    come from that. It comes from the intent's UNIQUE constraint: the intent id is
    derived from the slug and the window and is therefore identical across
    processes.
    """

    def test_a_fresh_engine_on_the_same_store_submits_no_second_order(
        self, tmp_path: Path
    ) -> None:
        first, store, executor = engine_at(tmp_path)
        market = live_market(store)
        first.open_market(market.slug, market.close_ts)
        now = inside_window(market)
        trigger_twap_support(first, market, now)
        executor.quote(market.slug, Direction.UP, Decimal("0.95"))
        executor.quote(market.slug, Direction.DOWN, Decimal("0.20"))
        asyncio.run(first.tick(market, healthy(), now + 0.2))
        assert len(store.orders_for(market.slug)) == 1
        store.close()

        # A new process: new engine, new in-memory state, same database file.
        second, reopened, executor2 = engine_at(tmp_path)
        second.open_market(market.slug, market.close_ts)
        second.restore_from_intents(market.slug, now)
        executor2.quote(market.slug, Direction.UP, Decimal("0.95"))
        executor2.quote(market.slug, Direction.DOWN, Decimal("0.20"))
        observe(market, Decimal("64001.00"), now + 1.0)
        asyncio.run(second.tick(market, healthy(), now + 1.0))
        asyncio.run(second.tick(market, healthy(), now + 1.2))

        assert len(reopened.intents_for(market.slug, engine=MAJORITY_ENGINE)) == 1
        assert len(reopened.orders_for(market.slug)) == 1, (
            "the restarted process must not place a second MAJORITY order"
        )
        state = second.state_for(market.slug)
        assert state is not None
        # The side lock was reconstructed from the persisted intent. The state
        # is SIDE_SELECTED (terminal-ish from the in-memory FSM's point of view;
        # not in MAJORITY_TERMINAL_STATES because PARTIAL/FILLED flow onward).
        assert state.state is MajorityState.SIDE_SELECTED
        assert state.selected_side is Direction.UP


# ── multi-window runtime ───────────────────────────────────────────────────────


def multi_window_engine_at(
    tmp_path: Path,
    *,
    windows: tuple[int, ...] = (3, 45),
    cfg: MajorityConfig | None = None,
    name: str = "arc.db",
) -> tuple[MajorityEngine, Store, PaperExecutor]:
    """A wired MAJORITY engine with multiple configured windows.

    `minimum` on the submitter is the minimum across all windows so a 3s and a
    45s window both fit. Mirrors the runtime's construction choice in
    `ArcRuntime.__init__`.
    """
    settings = cfg
    if settings is None:
        settings = MajorityConfig(
            enabled=True,
            windows=tuple(
                MajorityWindowConfig(
                    execution_window_seconds=w,
                    buffer=Decimal("1.00"),
                    trigger_price=Decimal("0.90"),
                    target_limit_price=Decimal("0.85"),
                    shares=Decimal("20"),
                    entry_price_min=Decimal("0.05"),
                    entry_price_max=Decimal("0.99"),
                )
                for w in windows
            ),
            # Buffer entry ON so the 3s window runs TWAP_SUPPORT and the 45s
            # window runs BTC_TRIGGER — the two modes this file exists to isolate.
            buffer_enabled=True,
        )
    store = store_at(tmp_path, name)
    executor = PaperExecutor()
    minimum = min(w.shares for w in settings.windows_by_offset)
    submitter = Submitter(
        store,
        executor,
        bucket=bucket(),
        minimum=minimum,
        engine=MAJORITY_ENGINE,
    )
    engine = MajorityEngine(settings, store, executor, submitter, tick_size=TICK)
    return engine, store, executor


class TestMultiWindowRuntime:
    """Two configured windows run independently for one market.

    The 3s window runs TWAP-support entry (window <= 30s), the 45s window runs
    the BTC buffer trigger (window > 30s) — both from the same configuration.
    """

    def test_two_windows_both_create_state(self, tmp_path: Path) -> None:
        engine, _, _ = multi_window_engine_at(tmp_path, windows=(3, 45))
        market = live_market(store_at(tmp_path))
        engine.open_market(market.slug, market.close_ts)
        assert engine.state_for(market.slug, 3) is not None
        assert engine.state_for(market.slug, 45) is not None

    def test_two_windows_produce_independent_orders(self, tmp_path: Path) -> None:
        """The 3s window fires off the TWAP support; the 45s window captures its
        BTC reference on the same tick and crosses its own level one tick later.
        Two triggers, two determinations, two orders."""
        engine, store, executor = multi_window_engine_at(tmp_path, windows=(3, 45))
        market = live_market(store)
        engine.open_market(market.slug, market.close_ts)

        # Both windows are open at close-1: the 45s window opened at close-45,
        # the 3s window at close-3.
        now = float(market.close_ts - 1)

        observe(market, Decimal("64001.00"), now)
        asyncio.run(engine.tick(market, healthy(), now))

        state3 = engine.state_for(market.slug, 3)
        state45 = engine.state_for(market.slug, 45)
        assert state3 is not None
        assert state45 is not None
        assert state3.triggered, "3s window: |TWAP - PTB| crossed the buffer"
        assert not state45.triggered, "45s window: reference captured, still monitoring"
        assert state45.btc_reference == Decimal("64001.00")
        assert state45.btc_up_trigger == Decimal("64002.00")
        assert state45.btc_down_trigger == Decimal("64000.00")

        executor.quote(market.slug, Direction.UP, Decimal("0.95"))
        executor.quote(market.slug, Direction.DOWN, Decimal("0.20"))
        observe(market, Decimal("64002.00"), now + 0.2)
        asyncio.run(engine.tick(market, healthy(), now + 0.2))
        # 3s window determined and submitted; 45s window's level just crossed.
        asyncio.run(engine.tick(market, healthy(), now + 0.4))

        intents = store.intents_for(market.slug, engine=MAJORITY_ENGINE)
        assert len(intents) == 2, f"expected two MAJORITY intents, got {len(intents)}"
        offsets = sorted(i.offset_seconds for i in intents)
        assert offsets == [3, 45]
        orders = store.orders_for(market.slug)
        assert len(orders) == 2
        offsets_in_orders = sorted(o.offset_seconds for o in orders)
        assert offsets_in_orders == [3, 45]

    def test_per_window_isolation_one_window_NO_TRADE_does_not_block_another(
        self, tmp_path: Path
    ) -> None:
        """A tied fresh read makes BOTH windows NO_TRADE, each on its own state."""
        engine, store, executor = multi_window_engine_at(tmp_path, windows=(3, 45))
        market = live_market(store)
        engine.open_market(market.slug, market.close_ts)
        now = float(market.close_ts - 1)

        observe(market, Decimal("64001.00"), now)
        asyncio.run(engine.tick(market, healthy(), now))
        # 3s fired; 45s monitoring with reference 64001.

        executor.quote(market.slug, Direction.UP, Decimal("0.50"))
        executor.quote(market.slug, Direction.DOWN, Decimal("0.50"))
        observe(market, Decimal("64002.00"), now + 0.2)
        asyncio.run(engine.tick(market, healthy(), now + 0.2))
        # 3s determined INDETERMINATE; 45s level crossed.
        asyncio.run(engine.tick(market, healthy(), now + 0.4))

        states = {s.execution_window_seconds: s for s in engine.states_for_market(market.slug)}
        assert states[3].state is MajorityState.NO_TRADE
        assert states[45].state is MajorityState.NO_TRADE
        assert store.intents_for(market.slug, engine=MAJORITY_ENGINE) == ()
        assert store.orders_for(market.slug) == ()


class TestPersistentSideLockRuntime:
    """Restart does not re-derive the side and does not orphan the resting order."""

    def test_a_restarted_engine_with_a_persisted_intent_locks_the_side(
        self, tmp_path: Path
    ) -> None:
        first, store, executor = engine_at(tmp_path)
        market = live_market(store)
        first.open_market(market.slug, market.close_ts)
        now = inside_window(market)
        trigger_twap_support(first, market, now)
        executor.quote(market.slug, Direction.UP, Decimal("0.95"))
        executor.quote(market.slug, Direction.DOWN, Decimal("0.20"))
        asyncio.run(first.tick(market, healthy(), now + 0.2))
        assert len(store.orders_for(market.slug)) == 1
        store.close()

        second, reopened, executor2 = engine_at(tmp_path)
        second.open_market(market.slug, market.close_ts)
        second.restore_from_intents(market.slug, now=now + 100.0)

        state = second.state_for(market.slug)
        assert state is not None
        assert state.selected_side is Direction.UP
        assert state.side_locked is True

        # A tick on the restarted engine must NOT place a second order: the
        # reconstructed side lock + the persisted intent UNIQUE constraint are
        # both engaged.
        executor2.quote(market.slug, Direction.UP, Decimal("0.95"))
        executor2.quote(market.slug, Direction.DOWN, Decimal("0.20"))
        asyncio.run(second.tick(market, healthy(), now + 200.0))
        asyncio.run(second.tick(market, healthy(), now + 200.2))
        assert len(reopened.intents_for(market.slug, engine=MAJORITY_ENGINE)) == 1
        assert len(reopened.orders_for(market.slug)) == 1


# ── price retry (spec O, P, Q, R) ────────────────────────────────────────────


class TestThePriceRetrySwitchInRuntime:
    """Spec O: the runtime builds one repricer per tradable window when the
    switch is ON, and none at all when it is OFF."""

    @staticmethod
    def _runtime_for(tmp_path: Path, majority: MajorityConfig) -> ArcRuntime:
        base = Settings(
            env=ArcSettings(_env_file=None),  # type: ignore[call-arg]
            trading=build_trading_config(dict(VALID_TRADING_VALUES)),
            seeded_from_env=False,
        )
        store = Store(tmp_path / "runtime.db")
        store.migrate(1_754_400_000.0)
        clock = FrozenClock(1_754_400_000.0)
        runtime = RuntimeState(store, clock)
        runtime.load()
        return ArcRuntime(
            settings=replace(base, majority=majority),
            store=store,
            clock=clock,
            runtime=runtime,
            discovery=build_discovery(),
            feed=RtdsFeed(clock),
            executor=PaperExecutor(),
            out=io.StringIO(),
        )

    def test_switch_off_builds_no_repricers(self, tmp_path: Path) -> None:
        run = self._runtime_for(tmp_path, config(price_retry_enabled=False))
        assert run._majority_repricers == {}

    def test_switch_on_builds_one_repricer_per_tradable_window(
        self, tmp_path: Path
    ) -> None:
        run = self._runtime_for(tmp_path, config(price_retry_enabled=True))
        assert set(run._majority_repricers) == {WINDOW}
        assert isinstance(run._majority_repricers[WINDOW], Repricer)


class TestPriceRetryPolicy:
    """Spec §11 arithmetic: one valid tick, PRICE only, band-bounded."""

    def _policy(self) -> RepricePolicy:
        return RepricePolicy(
            band_min=Decimal("0.05"), band_max=Decimal("0.99"), tick=TICK
        )

    def test_an_up_order_steps_up_one_tick(self) -> None:
        assert self._policy().target(
            Decimal("0.85"), Decimal("0.87"), Direction.UP
        ) == Decimal("0.86")

    def test_a_down_order_steps_down_one_tick(self) -> None:
        assert self._policy().target(
            Decimal("0.85"), Decimal("0.83"), Direction.DOWN
        ) == Decimal("0.84")

    def test_a_book_still_at_the_resting_price_stays_put(self) -> None:
        assert self._policy().target(Decimal("0.85"), Decimal("0.85"), Direction.UP) is None

    def test_an_unreadable_book_stays_put(self) -> None:
        assert self._policy().target(Decimal("0.85"), None, Direction.UP) is None

    def test_a_step_outside_the_band_refuses(self) -> None:
        assert self._policy().target(Decimal("0.99"), Decimal("0.98"), Direction.UP) is None

    def test_a_step_to_zero_or_below_refuses(self) -> None:
        assert self._policy().target(Decimal("0.01"), Decimal("0.02"), Direction.DOWN) is None


class TestPriceRetryPreservesTheOrder:
    """Spec Q/R: the successor keeps direction, market, window and engine; the
    step is exactly one venue tick; and the chain is capped."""

    def _repricer_at(
        self, tmp_path: Path, *, pre_reprice_attempts: int = 0
    ) -> tuple[Repricer, Store, PaperExecutor]:
        """pre_reprice_attempts=0 skips the same-price fill-priority gate (final
        spec §19/§20) so these tests isolate the reprice step itself. The gate
        has its own dedicated tests below."""
        store = store_at(tmp_path)
        executor = PaperExecutor()
        policy = RepricePolicy(
            band_min=Decimal("0.05"), band_max=Decimal("0.99"), tick=TICK
        )
        return (
            Repricer(
                store, executor, policy, bucket=bucket(),
                pre_reprice_attempts=pre_reprice_attempts,
            ),
            store,
            executor,
        )

    def _resting_order(
        self,
        store: Store,
        executor: PaperExecutor,
        market: MarketInstance,
        direction: Direction,
        price: Decimal,
    ) -> Order:
        order = new_order(
            market_slug=market.slug,
            offset_seconds=WINDOW,
            index=0,
            generation=0,
            direction=direction,
            price=price,
            size=Decimal("20"),
            now=1.0,
            engine=MAJORITY_ENGINE,
        )
        transition(order, OrderState.SUBMITTED, 1.0)
        store.save_order(order)
        # The paper executor refuses to cancel an order it never accepted.
        asyncio.run(executor.place(order))
        executor.quote(market.slug, direction, price)
        return order

    def test_the_successor_keeps_direction_market_window_and_engine(
        self, tmp_path: Path
    ) -> None:
        repricer, store, executor = self._repricer_at(tmp_path)
        market = make_market(store)
        order = self._resting_order(store, executor, market, Direction.UP, Decimal("0.85"))
        executor.quote(market.slug, Direction.UP, Decimal("0.87"))

        successor = asyncio.run(repricer.maybe_reprice(order, 2.0))

        assert successor is not order
        assert successor.direction is Direction.UP, "repricing never crosses direction"
        assert successor.market_slug == market.slug
        assert successor.offset_seconds == WINDOW
        assert successor.engine == MAJORITY_ENGINE
        assert successor.size == Decimal("20"), "price only, never quantity"
        assert order.state is OrderState.CANCELLED, "cancel-then-place, never amend"
        assert successor.state is OrderState.SUBMITTED

    def test_the_successor_steps_by_exactly_one_valid_tick(
        self, tmp_path: Path
    ) -> None:
        repricer, store, executor = self._repricer_at(tmp_path)
        market = make_market(store)
        order = self._resting_order(store, executor, market, Direction.UP, Decimal("0.85"))
        executor.quote(market.slug, Direction.UP, Decimal("0.87"))

        successor = asyncio.run(repricer.maybe_reprice(order, 2.0))

        assert successor.price - order.price == TICK
        assert successor.price % TICK == Decimal("0"), "on the venue tick grid"

    def test_a_down_order_steps_down_one_tick_keeping_direction(
        self, tmp_path: Path
    ) -> None:
        """Final spec Part 30: MAJORITY DOWN + retry → DOWN. The step is -1 tick
        and the successor keeps the DOWN direction."""
        repricer, store, executor = self._repricer_at(tmp_path)
        market = make_market(store)
        order = self._resting_order(store, executor, market, Direction.DOWN, Decimal("0.50"))
        executor.quote(market.slug, Direction.DOWN, Decimal("0.48"))

        successor = asyncio.run(repricer.maybe_reprice(order, 2.0))

        assert successor is not order
        assert successor.direction is Direction.DOWN
        assert order.price - successor.price == TICK

    def test_a_chain_at_the_retry_cap_stays_put(self, tmp_path: Path) -> None:
        repricer, store, executor = self._repricer_at(tmp_path)
        market = make_market(store)
        order = new_order(
            market_slug=market.slug,
            offset_seconds=WINDOW,
            index=0,
            generation=MAX_PRICE_RETRIES + 1,
            direction=Direction.UP,
            price=Decimal("0.85"),
            size=Decimal("20"),
            now=1.0,
            engine=MAJORITY_ENGINE,
        )
        transition(order, OrderState.SUBMITTED, 1.0)
        store.save_order(order)
        executor.quote(market.slug, Direction.UP, Decimal("0.87"))

        assert asyncio.run(repricer.maybe_reprice(order, 2.0)) is order
        assert order.state is OrderState.SUBMITTED


# ── fill priority: same price first (final spec §19/§20) ──────────────────────


class TestFillPriorityTriesSamePriceFirst:
    """An unfilled resting order gets `pre_reprice_attempts` polling passes at its
    current price BEFORE any cancel is considered (final spec §19/§20). The counter
    is per reprice chain and resets once a successor is placed."""

    def _repricer_with_attempts(
        self, tmp_path: Path, attempts: int
    ) -> tuple[Repricer, Store, PaperExecutor]:
        store = store_at(tmp_path)
        executor = PaperExecutor()
        policy = RepricePolicy(
            band_min=Decimal("0.05"), band_max=Decimal("0.99"), tick=TICK
        )
        return (
            Repricer(
                store, executor, policy, bucket=bucket(),
                pre_reprice_attempts=attempts,
            ),
            store,
            executor,
        )

    def _resting_up_order(
        self, store: Store, executor: PaperExecutor, market: MarketInstance
    ) -> Order:
        order = new_order(
            market_slug=market.slug,
            offset_seconds=WINDOW,
            index=0,
            generation=0,
            direction=Direction.UP,
            price=Decimal("0.85"),
            size=Decimal("20"),
            now=1.0,
            engine=MAJORITY_ENGINE,
        )
        transition(order, OrderState.SUBMITTED, 1.0)
        store.save_order(order)
        asyncio.run(executor.place(order))
        return order

    def test_the_order_rests_through_the_pre_attempts_even_with_a_moved_book(
        self, tmp_path: Path
    ) -> None:
        attempts = 3
        repricer, store, executor = self._repricer_with_attempts(tmp_path, attempts)
        market = make_market(store)
        order = self._resting_up_order(store, executor, market)
        # The book has already moved away: a reprice WOULD be valid, but must not
        # happen yet.
        executor.quote(market.slug, Direction.UP, Decimal("0.88"))

        for _ in range(attempts):
            result = asyncio.run(repricer.maybe_reprice(order, 2.0))
            assert result is order, "same-price attempts return the resting order"
            assert result.price == Decimal("0.85")
            assert result.state is OrderState.SUBMITTED

    def test_the_move_is_considered_only_after_the_pre_attempts(
        self, tmp_path: Path
    ) -> None:
        attempts = 3
        repricer, store, executor = self._repricer_with_attempts(tmp_path, attempts)
        market = make_market(store)
        order = self._resting_up_order(store, executor, market)
        executor.quote(market.slug, Direction.UP, Decimal("0.88"))

        for _ in range(attempts):
            asyncio.run(repricer.maybe_reprice(order, 2.0))

        successor = asyncio.run(repricer.maybe_reprice(order, 2.0))
        assert successor is not order, "after the pre-attempts, a move is allowed"
        assert successor.price - order.price == TICK
        assert order.state is OrderState.CANCELLED
        assert successor.state is OrderState.SUBMITTED

    def test_the_counter_resets_after_a_successor_is_placed(
        self, tmp_path: Path
    ) -> None:
        attempts = 2
        repricer, store, executor = self._repricer_with_attempts(tmp_path, attempts)
        market = make_market(store)
        order = self._resting_up_order(store, executor, market)
        executor.quote(market.slug, Direction.UP, Decimal("0.88"))

        # Exhaust the first set of attempts and place a successor.
        for _ in range(attempts):
            asyncio.run(repricer.maybe_reprice(order, 2.0))
        successor = asyncio.run(repricer.maybe_reprice(order, 2.0))
        assert successor is not order

        # The successor rests at the new price and gets its OWN set of attempts:
        # even though the book has moved again, it must rest through the reset
        # attempts before moving again.
        executor.quote(market.slug, Direction.UP, Decimal("0.90"))
        for _ in range(attempts):
            result = asyncio.run(repricer.maybe_reprice(successor, 2.0))
            assert result is successor, "the successor's counter was reset"
            assert result.price == Decimal("0.86")


# ── continuation under refusal (spec V, W, X) ────────────────────────────────


class TestTheRuntimeContinuesAfterARefusal:
    """Spec §14 / priority 7: a blocked or stale trade is skipped; the bot
    lives on and takes the next valid market. Never a shutdown."""

    def test_a_risk_blocked_market_does_not_stop_the_next_market(
        self, tmp_path: Path
    ) -> None:
        engine, store, executor = engine_at(tmp_path)
        blocked = live_market(store, 1754400000)
        engine.open_market(blocked.slug, blocked.close_ts)
        now = inside_window(blocked)
        trigger_twap_support(engine, blocked, now)
        executor.quote(blocked.slug, Direction.UP, Decimal("0.95"))
        executor.quote(blocked.slug, Direction.DOWN, Decimal("0.20"))
        unarmed = RuntimeHealth(
            trading_enabled=True,
            spec_status=SettlementSpecStatus.VERIFIED,
            execution_armed=False,
        )
        asyncio.run(engine.tick(blocked, unarmed, now + 0.2))

        blocked_state = engine.state_for(blocked.slug)
        assert blocked_state is not None
        assert blocked_state.state is MajorityState.NO_TRADE
        assert "G02" in blocked_state.no_trade_reason

        # The very next market, fully healthy, trades on the SAME engine.
        next_market = live_market(store, 1754400300)
        engine.open_market(next_market.slug, next_market.close_ts)
        now2 = inside_window(next_market)
        trigger_twap_support(engine, next_market, now2)
        executor.quote(next_market.slug, Direction.UP, Decimal("0.95"))
        executor.quote(next_market.slug, Direction.DOWN, Decimal("0.20"))
        asyncio.run(engine.tick(next_market, healthy(), now2 + 0.2))

        assert len(store.orders_for(next_market.slug)) == 1
        assert store.orders_for(blocked.slug) == ()

    def test_a_stale_feed_blocks_the_trade_but_not_the_bot(
        self, tmp_path: Path
    ) -> None:
        engine, store, executor = engine_at(tmp_path)
        stale = live_market(store, 1754400000)
        engine.open_market(stale.slug, stale.close_ts)
        now = inside_window(stale)
        trigger_twap_support(engine, stale, now)
        executor.quote(stale.slug, Direction.UP, Decimal("0.95"))
        executor.quote(stale.slug, Direction.DOWN, Decimal("0.20"))
        feed_blocked = RuntimeHealth(
            trading_enabled=True,
            spec_status=SettlementSpecStatus.VERIFIED,
            execution_armed=True,
            feed_blocked=True,
        )
        asyncio.run(engine.tick(stale, feed_blocked, now + 0.2))

        stale_state = engine.state_for(stale.slug)
        assert stale_state is not None
        assert stale_state.state is MajorityState.NO_TRADE
        assert "G14" in stale_state.no_trade_reason
        assert store.orders_for(stale.slug) == ()

        # Fresh feed, next market: trading resumes without a restart.
        next_market = live_market(store, 1754400300)
        engine.open_market(next_market.slug, next_market.close_ts)
        now2 = inside_window(next_market)
        trigger_twap_support(engine, next_market, now2)
        executor.quote(next_market.slug, Direction.UP, Decimal("0.95"))
        executor.quote(next_market.slug, Direction.DOWN, Decimal("0.20"))
        asyncio.run(engine.tick(next_market, healthy(), now2 + 0.2))

        assert len(store.orders_for(next_market.slug)) == 1

    def test_market_rotation_starts_the_next_market_from_zero(
        self, tmp_path: Path
    ) -> None:
        """Spec V: the first market trades, is dropped at close, and the next
        market trades on fresh state — nothing carries across the boundary."""
        engine, store, executor = engine_at(tmp_path)
        first = live_market(store, 1754400000)
        engine.open_market(first.slug, first.close_ts)
        now = inside_window(first)
        trigger_twap_support(engine, first, now)
        executor.quote(first.slug, Direction.UP, Decimal("0.95"))
        executor.quote(first.slug, Direction.DOWN, Decimal("0.20"))
        asyncio.run(engine.tick(first, healthy(), now + 0.2))
        assert len(store.orders_for(first.slug)) == 1

        engine.drop_market(first.slug)
        assert engine.state_for(first.slug) is None

        second = live_market(store, 1754400300)
        engine.open_market(second.slug, second.close_ts)
        now2 = inside_window(second)
        trigger_twap_support(engine, second, now2)
        executor.quote(second.slug, Direction.UP, Decimal("0.95"))
        executor.quote(second.slug, Direction.DOWN, Decimal("0.20"))
        asyncio.run(engine.tick(second, healthy(), now2 + 0.2))

        assert len(store.orders_for(second.slug)) == 1
        assert len(store.intents_for(second.slug, engine=MAJORITY_ENGINE)) == 1


# ── only MAJORITY can submit (spec Z, AA, AB) ────────────────────────────────


class TestOnlyMajorityCanSubmit:
    """TWAP remains as DATA for MAJORITY's entry support. It cannot submit an
    order, it cannot determine a direction, and nothing else in the tree can."""

    def test_every_artifact_of_a_trade_carries_majority_identity(
        self, tmp_path: Path
    ) -> None:
        _engine, store, executor, market, _now = trade_happy_path(tmp_path)

        orders = store.orders_for(market.slug)
        assert len(orders) == 1
        for order in orders:
            assert order.engine == MAJORITY_ENGINE
            assert order.order_id.startswith(f"{MAJORITY_ENGINE}:")

        intents = store.intents_for(market.slug, engine=MAJORITY_ENGINE)
        assert len(intents) == 1
        for intent in intents:
            assert intent.strategy_id == MAJORITY_ENGINE
        # The decision carried no TWAP numbers: the intent's TWAP fields are the
        # zero placeholders, because MAJORITY — not a TWAP rule — chose the side.
        assert intents[0].signal_twap == Decimal("0")
        assert intents[0].opening_twap == Decimal("0")

        # The fill path matches the resting order too.
        fills = executor.trade(market.slug, Decimal("0.80"), Decimal("20"))
        assert len(fills) == 1
        assert fills[0].price == Decimal("0.85")
        assert fills[0].size == Decimal("20")

    def test_no_twap_engine_module_remains_in_the_source_tree(self) -> None:
        """The TWAP trading engine (arc.strategy, arc.windows) was deleted, not
        disabled: no module remains, and nothing imports the old paths."""
        root = Path(__file__).resolve().parents[1]
        assert not (root / "arc" / "strategy").exists()
        assert not (root / "arc" / "windows").exists()
        offenders = [
            str(p)
            for p in (root / "arc").rglob("*.py")
            if "arc.strategy" in p.read_text(encoding="utf-8")
            or "arc.windows" in p.read_text(encoding="utf-8")
        ]
        assert not offenders


# ── V1/V2 parity (spec AC) ───────────────────────────────────────────────────


UP_TOKEN = "token-up"
DOWN_TOKEN = "token-down"


def _parity_book(token: str, bids: tuple[str, ...]) -> OrderBook:
    return OrderBook.model_validate(
        {
            "market": "0x" + "a" * 64,
            "asset_id": token,
            "timestamp": "0",
            "bids": [{"price": p, "size": "10"} for p in bids],
            "asks": [],
            "min_order_size": "5",
            "tick_size": "0.01",
            "neg_risk": False,
            "hash": "0xhash",
        }
    )


class _PerTokenVenue:
    """The scripted CLOB reduced to what a MAJORITY submission touches:
    placement acknowledgements and a per-token order book for the fresh read."""

    def __init__(self) -> None:
        self.books: dict[str, OrderBook] = {}
        self.place_result = AcceptedOrder(
            order_id=OrderId("venue-1"),
            status="live",
            making_amount=Decimal("17"),
            taking_amount=Decimal("20"),
            trade_ids=(),
            transactions_hashes=(),
        )

    async def place_limit_order(self, **kwargs: object) -> AcceptedOrder:
        return self.place_result

    async def get_order_book(self, *, token_id: str) -> OrderBook:
        return self.books[token_id]


def _tokens(market_slug: str, direction: Direction) -> str:
    return UP_TOKEN if direction is Direction.UP else DOWN_TOKEN


def _build_paper(store: Store) -> tuple[PaperExecutor, Callable[[str], None]]:
    """Adapter for the paper run: the book lives on the executor."""
    executor = PaperExecutor()

    def prepare(slug: str) -> None:
        executor.quote(slug, Direction.UP, Decimal("0.95"))
        executor.quote(slug, Direction.DOWN, Decimal("0.20"))

    return executor, prepare


def _build_live(store: Store) -> tuple[LiveExecutor, Callable[[str], None]]:
    """Adapter for the live run: the same numbers arrive as per-token venue books."""
    venue = _PerTokenVenue()
    venue.books[UP_TOKEN] = _parity_book(UP_TOKEN, ("0.95",))
    venue.books[DOWN_TOKEN] = _parity_book(DOWN_TOKEN, ("0.20",))
    executor = LiveExecutor(venue, _tokens, store.local_order_id)  # type: ignore[arg-type]
    return executor, lambda slug: None


def _parity_orders(
    tmp_path: Path,
    name: str,
    build: Callable[[Store], tuple[PaperExecutor | LiveExecutor, Callable[[str], None]]],
) -> tuple[tuple[Order, ...], tuple[ExecutionIntent, ...]]:
    """Run the identical MAJORITY sequence through one adapter.

    Returns (orders, intents). `build(store)` returns the adapter's executor and
    a callable that primes that adapter's book for one market slug.
    """
    settings = config()
    store = store_at(tmp_path, name)
    executor, prepare_book = build(store)
    minimum = min(w.shares for w in settings.windows_by_offset)
    engine = MajorityEngine(
        settings,
        store,
        executor,
        Submitter(store, executor, bucket=bucket(), minimum=minimum, engine=MAJORITY_ENGINE),
        tick_size=TICK,
    )
    market = live_market(store)
    engine.open_market(market.slug, market.close_ts)
    now = inside_window(market)
    trigger_twap_support(engine, market, now)
    prepare_book(market.slug)
    asyncio.run(engine.tick(market, healthy(), now + 0.2))
    return store.orders_for(market.slug), store.intents_for(market.slug, engine=MAJORITY_ENGINE)


class TestV1AndV2ProduceTheSameMajorityOrder:
    """Spec AC: the engine above the executor is byte-identical. The same entry,
    the same fresh read and the same gates must yield the same order on both
    adapters — only the venue id may differ."""

    def test_the_paper_and_live_orders_match_field_for_field(
        self, tmp_path: Path
    ) -> None:
        paper_orders, paper_intents = _parity_orders(tmp_path, "paper.db", _build_paper)
        live_orders, live_intents = _parity_orders(tmp_path, "live.db", _build_live)

        assert len(paper_orders) == len(live_orders) == 1
        fields: Callable[[Order], tuple[Direction, Decimal, Decimal, str, int]]
        fields = lambda o: (o.direction, o.price, o.size, o.engine, o.offset_seconds)  # noqa: E731
        assert fields(paper_orders[0]) == fields(live_orders[0])
        assert paper_intents[0].direction is live_intents[0].direction
        assert paper_intents[0].limit_price == live_intents[0].limit_price


# ── ledger + settlement identity (spec AD, AF) ───────────────────────────────


class TestTheLedgerCarriesMajorityIdentity:
    def test_the_record_for_a_majority_trade_names_the_majority_intent(
        self, tmp_path: Path
    ) -> None:
        # The ledger iterates the windows table. In production the runtime creates
        # each market with a window set that INCLUDES the MAJORITY offset (the
        # rotator unions trading offsets with MAJORITY's tradable windows), so a
        # row for offset 30 exists from market creation — PENDING, because
        # MAJORITY never freezes its window rows. Drive one such market to a
        # resting order and read the record back.
        engine, store, executor = engine_at(
            tmp_path, cfg=config(trigger_limit_enabled=True)
        )
        market = MarketInstance.create(1754400000, (*OFFSETS, WINDOW))
        store.create_market(market, float(market.window_ts))
        market.phase = MarketPhase.ACTIVE
        market.freeze_ptb(PTB)
        engine.open_market(market.slug, market.close_ts)

        now = inside_window(market)
        # Quote first so the trigger gate can latch (0.95 >= 0.90), final spec §12.
        executor.quote(market.slug, Direction.UP, Decimal("0.95"))
        executor.quote(market.slug, Direction.DOWN, Decimal("0.20"))
        trigger_twap_support(engine, market, now)
        asyncio.run(engine.tick(market, healthy(), now + 0.2))

        records = ledger_records(store)
        matching = [
            r for r in records if r.intent_id == majority_intent_id_for(market.slug, WINDOW)
        ]
        assert len(matching) == 1
        record = matching[0]
        # The window row is PENDING (MAJORITY does not freeze it); the direction
        # therefore surfaces from the persisted intent's locked side, not the row.
        assert record.direction == Direction.UP.value
        assert record.order_price == Decimal("0.85")
        assert record.quantity == Decimal("20")
        assert record.local_order_id.startswith(f"{MAJORITY_ENGINE}:")


class TestSettlementContinuesAfterAMajorityFill:
    """Spec AF: a filled MAJORITY order does not end the market's lifecycle.
    The market keeps running, the engine keeps ticking, and no second order
    comes out of the fill."""

    def test_a_counterparty_trade_fills_the_resting_order_and_life_goes_on(
        self, tmp_path: Path
    ) -> None:
        engine, store, executor, market, now = trade_happy_path(tmp_path)

        fills = executor.trade(market.slug, Decimal("0.80"), Decimal("20"))
        assert len(fills) == 1
        assert fills[0].size == Decimal("20")
        assert fills[0].price == Decimal("0.85"), "filled at the resting limit"

        # The market is still active and still accepts data...
        assert market.phase is MarketPhase.ACTIVE
        observe(market, Decimal("64002.00"), now + 1.0)
        # ...and the engine keeps ticking without producing a second order.
        asyncio.run(engine.tick(market, healthy(), now + 1.2))
        assert len(store.orders_for(market.slug)) == 1
        assert len(store.intents_for(market.slug, engine=MAJORITY_ENGINE)) == 1


# ── the 12-case switch matrix (final spec Part 27) ────────────────────────────

# (case number, window seconds, trigger+target ON, buffer ON, buffer value)
# Cases 1-6 are windows <= 30s; cases 7-12 are windows > 30s. Buffer values are
# BTC dollars for >30s and BTC dollars against the PTB for <=30s TWAP support.
MATRIX: tuple[tuple[int, int, bool, bool, Decimal], ...] = (
    (1, 30, False, False, Decimal("1.00")),
    (2, 30, False, True, Decimal("1.00")),
    (3, 30, False, True, Decimal("0")),
    (4, 30, True, False, Decimal("1.00")),
    (5, 30, True, True, Decimal("1.00")),
    (6, 30, True, True, Decimal("0")),
    (7, 45, False, False, Decimal("50")),
    (8, 45, False, True, Decimal("50")),
    (9, 45, False, True, Decimal("0")),
    (10, 45, True, False, Decimal("50")),
    (11, 45, True, True, Decimal("50")),
    (12, 45, True, True, Decimal("0")),
)


class TestTheTwelveCaseSwitchMatrix:
    """Final spec Part 27: every combination of window class, trigger switch and
    buffer switch, for MAJORITY UP and MAJORITY DOWN. The four-way matrix (Part
    13) decides WHICH gate runs; this matrix proves each cell produces exactly one
    order in the MAJORITY direction, priced by the switch that governs pricing."""

    @pytest.mark.parametrize("direction", [Direction.UP, Direction.DOWN])
    @pytest.mark.parametrize("case", MATRIX)
    def test_one_switch_combination_one_majority_order(
        self,
        tmp_path: Path,
        case: tuple[int, int, bool, bool, Decimal],
        direction: Direction,
    ) -> None:
        case_no, window, trig, buf_on, buf_val = case
        cfg = config(
            window=window,
            buffer=buf_val,
            trigger_limit_enabled=trig,
            buffer_enabled=buf_on,
        )
        engine, store, executor = engine_at(tmp_path, cfg=cfg)
        market = live_market(store)
        engine.open_market(market.slug, market.close_ts)
        now = inside_window(market, window=window)

        # The book that carries the MAJORITY direction for this case.
        up_quote = Decimal("0.95") if direction is Direction.UP else Decimal("0.20")
        down_quote = Decimal("0.20") if direction is Direction.UP else Decimal("0.95")

        # With the trigger switch ON the book must present the trigger price
        # BEFORE the trigger tick (final spec §12 ordering).
        if trig:
            executor.quote(market.slug, Direction.UP, up_quote)
            executor.quote(market.slug, Direction.DOWN, down_quote)

        # Drive the buffer gate for this case's entry mode.
        window_cfg = cfg.window_for(window)
        assert window_cfg is not None
        mode = EntryMode.for_window(window_cfg, buffer_enabled=buf_on)
        if mode is EntryMode.DIRECT:
            # No buffer condition: fires at window open, no mathematics.
            asyncio.run(engine.tick(market, healthy(), now))
        elif mode is EntryMode.BTC_TRIGGER:
            # Reference at 65000 (Part 28), then the direction's level crosses.
            observe(market, Decimal("65000.00"), now)
            asyncio.run(engine.tick(market, healthy(), now))
            cross = (
                Decimal("65050.00") if direction is Direction.UP else Decimal("64950.00")
            )
            observe(market, cross, now + 0.2)
            asyncio.run(engine.tick(market, healthy(), now + 0.2))
            now = now + 0.2
        else:  # TWAP_SUPPORT
            if buf_val > 0:
                spot = (
                    Decimal("64001.00") if direction is Direction.UP else Decimal("63999.00")
                )
            else:
                # Zero buffer: the TWAP reference is satisfied by any sample.
                spot = Decimal("64000.00")
            observe(market, spot, now)
            asyncio.run(engine.tick(market, healthy(), now))

        # Trigger OFF: the book is only read at the fresh read, not before.
        if not trig:
            executor.quote(market.slug, Direction.UP, up_quote)
            executor.quote(market.slug, Direction.DOWN, down_quote)
        asyncio.run(engine.tick(market, healthy(), now + 0.4))

        orders = store.orders_for(market.slug)
        assert len(orders) == 1, f"case {case_no}: expected exactly one order"
        assert orders[0].direction is direction, f"case {case_no}"
        assert orders[0].engine == MAJORITY_ENGINE, f"case {case_no}"
        assert orders[0].offset_seconds == window, f"case {case_no}"
        expected_price = (
            Decimal("0.85")
            if trig
            else (up_quote if direction is Direction.UP else down_quote)
        )
        assert orders[0].price == expected_price, (
            f"case {case_no}: ON -> configured target, OFF -> live best bid"
        )

    def test_trigger_points_down_but_majority_up_buys_up(self, tmp_path: Path) -> None:
        """Part 27's final clause: the trigger condition points one direction but
        MAJORITY points the opposite — the order follows MAJORITY. The DOWN bid
        crosses the trigger; before the fresh read the book flips and UP holds the
        majority. The UP order is the only correct outcome."""
        engine, store, executor = engine_at(
            tmp_path, cfg=config(trigger_limit_enabled=True, buffer_enabled=False)
        )
        market = live_market(store)
        engine.open_market(market.slug, market.close_ts)
        now = inside_window(market)

        # Trigger tick: DOWN crosses the trigger (0.92 >= 0.90).
        executor.quote(market.slug, Direction.UP, Decimal("0.50"))
        executor.quote(market.slug, Direction.DOWN, Decimal("0.92"))
        asyncio.run(engine.tick(market, healthy(), now))
        state = engine.state_for(market.slug)
        assert state is not None
        assert state.price_trigger_reached
        assert state.selected_side is None, "no side is chosen at the trigger instant"

        # The book reverses before the fresh read: MAJORITY is now UP.
        executor.quote(market.slug, Direction.UP, Decimal("0.95"))
        executor.quote(market.slug, Direction.DOWN, Decimal("0.20"))
        asyncio.run(engine.tick(market, healthy(), now + 0.2))

        orders = store.orders_for(market.slug)
        assert len(orders) == 1
        assert orders[0].direction is Direction.UP, "the order follows MAJORITY, not the trigger"


# ── the price trigger gate (final spec Part 33 audit) ─────────────────────────


class TestThePriceTriggerGate:
    """`majority_trigger_price` is wired: it is the configured Polymarket trigger
    price, waited on when the switch is ON, and never an order price."""

    def test_no_entry_before_the_trigger_price_is_reached(self, tmp_path: Path) -> None:
        engine, store, executor = engine_at(
            tmp_path, cfg=config(trigger_limit_enabled=True, buffer_enabled=False)
        )
        market = live_market(store)
        engine.open_market(market.slug, market.close_ts)
        now = inside_window(market)
        # Both bids below the 0.90 trigger: the window must wait.
        executor.quote(market.slug, Direction.UP, Decimal("0.89"))
        executor.quote(market.slug, Direction.DOWN, Decimal("0.05"))
        asyncio.run(engine.tick(market, healthy(), now))

        state = engine.state_for(market.slug)
        assert state is not None
        assert state.state is MajorityState.WAITING_TRIGGER
        assert not state.price_trigger_reached
        assert not state.triggered
        assert store.orders_for(market.slug) == ()

    def test_the_trigger_latches_and_a_book_reversal_does_not_unfire_it(
        self, tmp_path: Path
    ) -> None:
        engine, store, executor = engine_at(
            tmp_path, cfg=config(trigger_limit_enabled=True, buffer_enabled=False)
        )
        market = live_market(store)
        engine.open_market(market.slug, market.close_ts)
        now = inside_window(market)
        executor.quote(market.slug, Direction.UP, Decimal("0.95"))
        executor.quote(market.slug, Direction.DOWN, Decimal("0.20"))
        asyncio.run(engine.tick(market, healthy(), now))

        state = engine.state_for(market.slug)
        assert state is not None
        assert state.price_trigger_reached
        assert state.triggered, "buffer OFF: DIRECT fires once the trigger latches"

        # The book falls back below the trigger; the latch must hold and the
        # fresh read still completes the order.
        executor.quote(market.slug, Direction.UP, Decimal("0.89"))
        executor.quote(market.slug, Direction.DOWN, Decimal("0.05"))
        asyncio.run(engine.tick(market, healthy(), now + 0.2))

        orders = store.orders_for(market.slug)
        assert len(orders) == 1
        assert state.price_trigger_reached, "a latch is never un-fired"


# ── BTC buffer levels are memory-only (final spec Parts 28 + 29) ──────────────


class TestBtcBufferLevelsAreMemoryOnly:
    """Part 28: BTC 65000 with buffer 50 captures UP=65050 / DOWN=64950 in
    memory. These levels are entry conditions only: never Polymarket orders, never
    order prices, and the current MAJORITY still decides the final side."""

    def test_levels_are_captured_and_no_fake_orders_are_created(
        self, tmp_path: Path
    ) -> None:
        engine, store, _executor = engine_at(
            tmp_path, cfg=config(window=45, buffer=Decimal("50"))
        )
        market = live_market(store)
        engine.open_market(market.slug, market.close_ts)
        now = inside_window(market, window=45)
        observe(market, Decimal("65000.00"), now)
        asyncio.run(engine.tick(market, healthy(), now))

        state = engine.state_for(market.slug)
        assert state is not None
        assert state.btc_reference == Decimal("65000.00")
        assert state.btc_up_trigger == Decimal("65050.00")
        assert state.btc_down_trigger == Decimal("64950.00")
        assert store.orders_for(market.slug) == (), "levels are memory, not orders"
        assert store.intents_for(market.slug, engine=MAJORITY_ENGINE) == ()

    def test_up_trigger_fires_and_majority_up_trades_up_at_a_share_price(
        self, tmp_path: Path
    ) -> None:
        engine, store, executor = engine_at(
            tmp_path, cfg=config(window=45, buffer=Decimal("50"))
        )
        market = live_market(store)
        engine.open_market(market.slug, market.close_ts)
        now = inside_window(market, window=45)
        observe(market, Decimal("65000.00"), now)
        asyncio.run(engine.tick(market, healthy(), now))
        observe(market, Decimal("65050.00"), now + 0.2)  # exactly the level: >=
        asyncio.run(engine.tick(market, healthy(), now + 0.2))
        executor.quote(market.slug, Direction.UP, Decimal("0.95"))
        executor.quote(market.slug, Direction.DOWN, Decimal("0.20"))
        asyncio.run(engine.tick(market, healthy(), now + 0.4))

        orders = store.orders_for(market.slug)
        assert len(orders) == 1
        assert orders[0].direction is Direction.UP
        # No token-price confusion: the order price is a share price, never the
        # BTC level that opened the opportunity.
        assert orders[0].price == Decimal("0.95")
        assert orders[0].price < Decimal("1")

    def test_down_trigger_fires_and_majority_down_trades_down_at_a_share_price(
        self, tmp_path: Path
    ) -> None:
        engine, store, executor = engine_at(
            tmp_path, cfg=config(window=45, buffer=Decimal("50"))
        )
        market = live_market(store)
        engine.open_market(market.slug, market.close_ts)
        now = inside_window(market, window=45)
        observe(market, Decimal("65000.00"), now)
        asyncio.run(engine.tick(market, healthy(), now))
        observe(market, Decimal("64950.00"), now + 0.2)
        asyncio.run(engine.tick(market, healthy(), now + 0.2))
        executor.quote(market.slug, Direction.UP, Decimal("0.20"))
        executor.quote(market.slug, Direction.DOWN, Decimal("0.95"))
        asyncio.run(engine.tick(market, healthy(), now + 0.4))

        orders = store.orders_for(market.slug)
        assert len(orders) == 1
        assert orders[0].direction is Direction.DOWN
        assert orders[0].price == Decimal("0.95")
        assert orders[0].price < Decimal("1")


class TestTwapDataIsSupportOnly:
    """Part 29: for <=30s windows the TWAP ± buffer conditions are memory/entry
    conditions. TWAP cannot submit, cannot choose a direction, and its numbers
    never become order prices."""

    def test_twap_values_never_become_order_prices(self, tmp_path: Path) -> None:
        engine, store, executor = engine_at(tmp_path)  # window 30, trigger OFF
        market = live_market(store)
        engine.open_market(market.slug, market.close_ts)
        now = inside_window(market)
        observe(market, Decimal("64001.00"), now)  # |TWAP - PTB| crosses buffer
        asyncio.run(engine.tick(market, healthy(), now))
        executor.quote(market.slug, Direction.UP, Decimal("0.95"))
        executor.quote(market.slug, Direction.DOWN, Decimal("0.20"))
        asyncio.run(engine.tick(market, healthy(), now + 0.2))

        orders = store.orders_for(market.slug)
        assert len(orders) == 1
        assert orders[0].price == Decimal("0.95"), "the live best bid, a share price"
        assert orders[0].price != Decimal("64001.00")
        assert orders[0].price < Decimal("1")

        # The intent carries honest zero TWAP fields: MAJORITY chose the side,
        # not a TWAP rule.
        intents = store.intents_for(market.slug, engine=MAJORITY_ENGINE)
        assert intents[0].signal_twap == Decimal("0")
        assert intents[0].opening_twap == Decimal("0")

    def test_a_twap_crossing_without_a_majority_reads_no_side(
        self, tmp_path: Path
    ) -> None:
        """The TWAP condition firing is only WHEN. With no usable book afterwards
        there is no majority to read and no order to place — TWAP never supplies a
        direction of its own."""
        engine, store, executor = engine_at(tmp_path)
        market = live_market(store)
        engine.open_market(market.slug, market.close_ts)
        now = inside_window(market)
        observe(market, Decimal("64001.00"), now)
        asyncio.run(engine.tick(market, healthy(), now))

        executor.forget(market.slug)  # unreadable book at the fresh read
        asyncio.run(engine.tick(market, healthy(), now + 0.2))

        state = engine.state_for(market.slug)
        assert state is not None
        assert state.state is MajorityState.NO_TRADE
        assert state.selected_side is None
        assert store.orders_for(market.slug) == ()


# ── nine-window isolation (final spec Part 31) ────────────────────────────────


class TestNineWindowsMaintainIndependentState:
    """Ten through 180 seconds open simultaneously. Each window keeps its own
    trigger/buffer/direction/order state; firing one window never moves another."""

    WINDOWS: tuple[int, ...] = (10, 20, 31, 45, 49, 60, 90, 120, 180)

    def _cfg(self) -> MajorityConfig:
        """Per-window buffers chosen so each window's entry condition is reachable
        independently. The two short windows (10, 20) run TWAP_SUPPORT with a buffer
        far beyond any BTC move this test makes, so they stay monitoring. The long
        windows run BTC_TRIGGER with strictly ascending, well-separated levels so a
        single crossing fires exactly the window whose level is met — proving one
        window firing never pulls another along."""
        # buffer -> BTC trigger level = reference(64000) + buffer
        buffers = {
            31: Decimal("10"),   # level 64010
            45: Decimal("20"),   # level 64020
            49: Decimal("30"),   # level 64030
            60: Decimal("40"),   # level 64040
            90: Decimal("50"),   # level 64050
            120: Decimal("60"),  # level 64060
            180: Decimal("70"),  # level 64070
        }
        return MajorityConfig(
            enabled=True,
            windows=tuple(
                MajorityWindowConfig(
                    execution_window_seconds=w,
                    buffer=buffers.get(w, Decimal("1000")),
                    trigger_price=Decimal("0.90"),
                    target_limit_price=Decimal("0.85"),
                    shares=Decimal("20"),
                    entry_price_min=Decimal("0.05"),
                    entry_price_max=Decimal("0.99"),
                )
                for w in self.WINDOWS
            ),
            buffer_enabled=True,
        )

    def test_all_nine_windows_open_with_independent_waiting_state(
        self, tmp_path: Path
    ) -> None:
        engine, store, _ = multi_window_engine_at(
            tmp_path, windows=self.WINDOWS, cfg=self._cfg()
        )
        market = live_market(store)
        engine.open_market(market.slug, market.close_ts)
        now = float(market.close_ts - 1)  # every window is open at close-1
        observe(market, Decimal("64000.00"), now)
        asyncio.run(engine.tick(market, healthy(), now))

        states = engine.states_for_market(market.slug)
        assert len(states) == 9
        for state in states:
            assert state.state is MajorityState.WAITING_TRIGGER
            assert not state.triggered
            assert state.selected_side is None
        assert store.orders_for(market.slug) == ()

    def test_firing_one_window_never_fires_another(self, tmp_path: Path) -> None:
        """Cross one window's BTC level at a time and assert the exact fired set.
        Each crossing fires precisely the window whose level is met and leaves every
        other window — short TWAP-support windows included — untouched. That is the
        isolation property: one window's trigger/buffer/direction/order never leaks
        into another window's state."""
        engine, store, executor = multi_window_engine_at(
            tmp_path, windows=self.WINDOWS, cfg=self._cfg()
        )
        market = live_market(store)
        engine.open_market(market.slug, market.close_ts)
        now = float(market.close_ts - 1)
        observe(market, Decimal("64000.00"), now)
        asyncio.run(engine.tick(market, healthy(), now))

        executor.quote(market.slug, Direction.UP, Decimal("0.95"))
        executor.quote(market.slug, Direction.DOWN, Decimal("0.20"))

        # ── fire ONLY the 31s window: BTC crosses its +10 level (64010) ──
        observe(market, Decimal("64010.00"), now + 0.2)
        asyncio.run(engine.tick(market, healthy(), now + 0.2))  # 31 fires
        asyncio.run(engine.tick(market, healthy(), now + 0.4))  # 31 submits

        orders = store.orders_for(market.slug)
        assert len(orders) == 1
        assert orders[0].offset_seconds == 31
        assert orders[0].direction is Direction.UP
        states = {s.execution_window_seconds: s for s in engine.states_for_market(market.slug)}
        assert states[31].state is MajorityState.SUBMITTED
        for w in self.WINDOWS:
            if w != 31:
                assert not states[w].triggered, (
                    f"window {w} fired when only the 31s window's level was crossed"
                )

        # ── fire ONLY the 45s window: BTC crosses its +20 level (64020) ──
        observe(market, Decimal("64020.00"), now + 0.6)
        asyncio.run(engine.tick(market, healthy(), now + 0.6))  # 45 fires
        asyncio.run(engine.tick(market, healthy(), now + 0.8))  # 45 submits

        orders = store.orders_for(market.slug)
        assert len(orders) == 2
        assert sorted(o.offset_seconds for o in orders) == [31, 45]
        states = {s.execution_window_seconds: s for s in engine.states_for_market(market.slug)}
        assert states[45].state is MajorityState.SUBMITTED
        assert states[31].state is MajorityState.SUBMITTED, "31 did not re-fire"
        for w in self.WINDOWS:
            if w not in (31, 45):
                assert not states[w].triggered, (
                    f"window {w} fired without its own buffer condition being met"
                )
        assert len(store.intents_for(market.slug, engine=MAJORITY_ENGINE)) == 2


# ── the retry switch gate and attempt count (final spec Parts 20/22) ──────────


class TestThePriceRetryGateAndAttemptCount:
    """Repricers exist only while retry is ON and the trigger/target switch is
    OFF; each one carries the configured pre-repricing attempt count."""

    def test_no_repricers_when_the_trigger_switch_is_on(self, tmp_path: Path) -> None:
        run = TestThePriceRetrySwitchInRuntime._runtime_for(
            tmp_path,
            config(price_retry_enabled=True, trigger_limit_enabled=True),
        )
        assert run._majority_repricers == {}, (
            "+1/-1 repricing applies ONLY while the trigger/target switch is OFF"
        )

    def test_repricers_carry_the_configured_attempt_count(self, tmp_path: Path) -> None:
        run = TestThePriceRetrySwitchInRuntime._runtime_for(
            tmp_path,
            config(price_retry_enabled=True, price_retry_attempts=8),
        )
        repricer = run._majority_repricers[WINDOW]
        assert repricer._pre_attempts == 8


# ── paper fill identity (final spec Part 32) ──────────────────────────────────


class TestPaperFillIdentity:
    """Part 32: no fill may be recorded under the TWAP default. Paper fills
    derive the engine from the order id, and venue-reported fills inherit the
    engine of the order they filled."""

    def test_a_paper_fill_of_a_majority_order_carries_majority_identity(
        self, tmp_path: Path
    ) -> None:
        _engine, store, executor, market, _now = trade_happy_path(tmp_path)

        fills = executor.trade(market.slug, Decimal("0.80"), Decimal("20"))
        assert len(fills) == 1
        assert fills[0].engine == MAJORITY_ENGINE, "not the model's TWAP default"
        assert fills[0].order_id.startswith(f"{MAJORITY_ENGINE}:")

        stored = store.fills_for(market.slug) if store.fills_for(market.slug) else fills
        assert all(f.engine == MAJORITY_ENGINE for f in stored)

    def test_venue_reported_fills_inherit_the_engine_of_their_order(
        self, tmp_path: Path
    ) -> None:
        """FillEngine.ingest: a venue fill arrives with no engine column; it must
        take the engine of the order it filled, never the TWAP default."""
        _engine, store, executor, market, _now = trade_happy_path(tmp_path)
        order = store.orders_for(market.slug)[0]
        assert order.engine == MAJORITY_ENGINE

        fill_engine = FillEngine(store, executor)
        reported = (
            Fill(
                fill_id="venue-fill-1",
                order_id=order.order_id,
                market_slug=market.slug,
                size=Decimal("20"),
                price=order.price,
                ts=0.0,
                # engine deliberately omitted: the model default is TWAP, and
                # ingest must overwrite it from the order.
            ),
        )
        report = fill_engine.ingest(market.slug, reported, 1.0)
        assert len(report.new_fills) == 1
        assert report.new_fills[0].engine == MAJORITY_ENGINE
        stored = store.fills_for(market.slug)
        assert len(stored) == 1
        assert stored[0].engine == MAJORITY_ENGINE
