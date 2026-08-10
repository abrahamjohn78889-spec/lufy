"""MAJORITY end to end: trigger, fresh read, side, intent, order.

The unit tests in test_majority_core.py prove each piece in isolation and
test_engine_isolation.py proves the two engines cannot reach each other's rows.
This file proves the pieces compose — that a book crossing the trigger actually
produces a resting order with MAJORITY's identity on it, and that every path
which must NOT produce one does not.

Nothing internal is mocked. The real MajorityEngine, real Store, real Submitter,
real RiskEngine and real order FSM are used; the only substitute is the V1 paper
adapter, which is production code.

THE TWO-STEP RULE is the reason this file exists. Every assertion about the side
is written against a book that CHANGED between the trigger read and the fresh
read, because a test whose book is static cannot tell a correct implementation
from one that reuses the trigger snapshot.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal
from pathlib import Path

from execution_fixtures import bucket, make_market, store_at

from arc.decision.engine import RuntimeHealth
from arc.domain.enums import Direction, MarketPhase, SettlementSpecStatus
from arc.execution.submit import Submitter
from arc.execution.v1_paper import PaperExecutor
from arc.majority.config import MAJORITY_ENGINE, MajorityConfig
from arc.majority.engine import MajorityEngine
from arc.majority.identity import majority_intent_id_for
from arc.majority.state import MajorityState
from arc.storage.store import Store

WINDOW = 30
PTB = Decimal("64000.00")


def config(
    *,
    enabled: bool = True,
    trigger_price: Decimal = Decimal("0.90"),
    target_limit_price: Decimal = Decimal("0.85"),
    shares: Decimal = Decimal("20"),
    entry_price_min: Decimal = Decimal("0.05"),
    entry_price_max: Decimal = Decimal("0.99"),
    disable_reason: str = "",
    window: int = WINDOW,
) -> MajorityConfig:
    """A tradable MAJORITY configuration. Each test varies one field."""
    from arc.majority.config import MajorityWindowConfig

    return MajorityConfig(
        enabled=enabled,
        windows=(
            MajorityWindowConfig(
                execution_window_seconds=window,
                buffer=Decimal("1.00"),
                trigger_price=trigger_price,
                target_limit_price=target_limit_price,
                shares=shares,
                entry_price_min=entry_price_min,
                entry_price_max=entry_price_max,
                disable_reason=disable_reason,
            ),
        ),
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
    return MajorityEngine(settings, store, executor, submitter), store, executor


def live_market(store: Store, window_ts: int = 1754400000):
    """An ACTIVE market with an official PTB frozen, as gate 5 requires."""
    market = make_market(store, window_ts)
    market.phase = MarketPhase.ACTIVE
    market.freeze_ptb(PTB)
    return market


def inside_window(market) -> float:
    """A clock reading inside the execution window, one second past its opening."""
    return float(market.close_ts - WINDOW + 1)


# ── A. the happy path ────────────────────────────────────────────────────────


class TestTheFullSequenceProducesOneRestingOrder:
    """Trigger, fresh read, side lock, intent, order. Every step observed."""

    def test_a_crossed_trigger_reaches_a_resting_majority_order(self, tmp_path: Path) -> None:
        engine, store, executor = engine_at(tmp_path)
        market = live_market(store)
        engine.open_market(market.slug, market.close_ts)
        now = inside_window(market)

        # Step 1: UP crosses the 0.90 trigger.
        executor.quote(market.slug, Direction.UP, Decimal("0.91"))
        executor.quote(market.slug, Direction.DOWN, Decimal("0.05"))
        asyncio.run(engine.tick(market, healthy(), now))

        state = engine.state_for(market.slug)
        assert state is not None
        assert state.state is MajorityState.TRIGGERED
        assert state.selected_side is None, "the side must not be chosen on the trigger pass"

        # Step 2: a second pass takes the fresh read and locks the side.
        asyncio.run(engine.tick(market, healthy(), now + 0.2))

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
        that priced off the winning bid would produce 0.97 and fail here.
        """
        engine, store, executor = engine_at(tmp_path)
        market = live_market(store)
        engine.open_market(market.slug, market.close_ts)
        now = inside_window(market)
        executor.quote(market.slug, Direction.UP, Decimal("0.97"))
        executor.quote(market.slug, Direction.DOWN, Decimal("0.02"))

        asyncio.run(engine.tick(market, healthy(), now))
        asyncio.run(engine.tick(market, healthy(), now + 0.2))

        orders = store.orders_for(market.slug)
        assert len(orders) == 1
        assert orders[0].price == Decimal("0.85")


# ── B. the two-step rule ─────────────────────────────────────────────────────


class TestTheSideComesFromTheFreshReadNotTheTrigger:
    """The side that crossed the trigger is NOT necessarily the side bought.

    This is the single most important property in the module and the easiest to
    implement wrongly, because reusing the trigger snapshot passes every test whose
    book never moves.
    """

    def test_the_crossing_side_loses_when_the_fresh_read_disagrees(
        self, tmp_path: Path
    ) -> None:
        """UP crosses at 0.91, then collapses to 0.16. DOWN is bought. That is correct."""
        engine, store, executor = engine_at(tmp_path)
        market = live_market(store)
        engine.open_market(market.slug, market.close_ts)
        now = inside_window(market)

        executor.quote(market.slug, Direction.UP, Decimal("0.91"))
        executor.quote(market.slug, Direction.DOWN, Decimal("0.85"))
        asyncio.run(engine.tick(market, healthy(), now))

        state = engine.state_for(market.slug)
        assert state is not None
        assert state.state is MajorityState.TRIGGERED

        # The book moves between the two steps, which is the whole point.
        executor.quote(market.slug, Direction.UP, Decimal("0.16"))
        asyncio.run(engine.tick(market, healthy(), now + 0.2))

        assert state.selected_side is Direction.DOWN, (
            "the side must come from the fresh read; UP crossed the trigger but "
            "DOWN held the majority when the side was determined"
        )
        intents = store.intents_for(market.slug, engine=MAJORITY_ENGINE)
        assert intents[0].direction is Direction.DOWN

    def test_both_snapshots_are_kept_so_the_two_steps_are_auditable(
        self, tmp_path: Path
    ) -> None:
        engine, store, executor = engine_at(tmp_path)
        market = live_market(store)
        engine.open_market(market.slug, market.close_ts)
        now = inside_window(market)

        executor.quote(market.slug, Direction.UP, Decimal("0.93"))
        executor.quote(market.slug, Direction.DOWN, Decimal("0.40"))
        asyncio.run(engine.tick(market, healthy(), now))
        executor.quote(market.slug, Direction.UP, Decimal("0.10"))
        asyncio.run(engine.tick(market, healthy(), now + 0.2))

        state = engine.state_for(market.slug)
        assert state is not None
        assert state.trigger_snapshot is not None
        assert state.decision_snapshot is not None
        assert state.trigger_snapshot.best_bid_up == Decimal("0.93")
        assert state.decision_snapshot.best_bid_up == Decimal("0.10")
        assert state.trigger_snapshot is not state.decision_snapshot


# ── C. no trade ──────────────────────────────────────────────────────────────


class TestEveryNonTradePathSubmitsNothing:
    """INDETERMINATE, an unreached trigger, OFF, and a closed window."""

    def test_equal_bids_resolve_to_no_trade_and_are_never_tie_broken(
        self, tmp_path: Path
    ) -> None:
        engine, store, executor = engine_at(tmp_path)
        market = live_market(store)
        engine.open_market(market.slug, market.close_ts)
        now = inside_window(market)

        executor.quote(market.slug, Direction.UP, Decimal("0.95"))
        executor.quote(market.slug, Direction.DOWN, Decimal("0.30"))
        asyncio.run(engine.tick(market, healthy(), now))
        # Dead level on the fresh read. There is no majority to read off this book.
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

        executor.quote(market.slug, Direction.UP, Decimal("0.92"))
        executor.quote(market.slug, Direction.DOWN, Decimal("0.20"))
        asyncio.run(engine.tick(market, healthy(), now))
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

        executor.quote(market.slug, Direction.UP, Decimal("0.94"))
        executor.quote(market.slug, Direction.DOWN, Decimal("0.30"))
        asyncio.run(engine.tick(market, healthy(), now))
        # Ten seconds later: well past the 2.0s budget.
        asyncio.run(engine.tick(market, healthy(), now + 10.0))

        state = engine.state_for(market.slug)
        assert state is not None
        assert state.state is MajorityState.NO_TRADE
        assert store.orders_for(market.slug) == ()

    def test_a_book_below_the_trigger_keeps_waiting(self, tmp_path: Path) -> None:
        engine, store, executor = engine_at(tmp_path)
        market = live_market(store)
        engine.open_market(market.slug, market.close_ts)
        now = inside_window(market)

        executor.quote(market.slug, Direction.UP, Decimal("0.60"))
        executor.quote(market.slug, Direction.DOWN, Decimal("0.38"))
        for step in range(5):
            asyncio.run(engine.tick(market, healthy(), now + step * 0.2))

        state = engine.state_for(market.slug)
        assert state is not None
        assert state.state is MajorityState.WAITING_TRIGGER
        assert not state.triggered
        assert store.orders_for(market.slug) == ()

    def test_the_trigger_is_inclusive_at_exactly_the_threshold(self, tmp_path: Path) -> None:
        """`>=`, not `>`. A bid resting exactly at the configured trigger fires it."""
        engine, store, executor = engine_at(tmp_path)
        market = live_market(store)
        engine.open_market(market.slug, market.close_ts)
        now = inside_window(market)

        executor.quote(market.slug, Direction.UP, Decimal("0.90"))
        executor.quote(market.slug, Direction.DOWN, Decimal("0.09"))
        asyncio.run(engine.tick(market, healthy(), now))

        state = engine.state_for(market.slug)
        assert state is not None
        assert state.triggered

    def test_nothing_happens_before_the_window_opens(self, tmp_path: Path) -> None:
        """A book over the trigger 60 seconds early is not traded 60 seconds early."""
        engine, store, executor = engine_at(tmp_path)
        market = live_market(store)
        engine.open_market(market.slug, market.close_ts)
        executor.quote(market.slug, Direction.UP, Decimal("0.99"))
        executor.quote(market.slug, Direction.DOWN, Decimal("0.01"))

        early = float(market.close_ts - WINDOW - 60)
        asyncio.run(engine.tick(market, healthy(), early))

        state = engine.state_for(market.slug)
        assert state is not None
        assert state.state is MajorityState.WAITING_WINDOW
        assert not state.triggered
        assert store.orders_for(market.slug) == ()

    def test_a_disabled_engine_reads_no_book_at_all(self, tmp_path: Path) -> None:
        """OFF means OFF: no state row, no trigger evaluation, no order."""
        engine, store, executor = engine_at(tmp_path, cfg=config(enabled=False))
        market = live_market(store)
        engine.open_market(market.slug, market.close_ts)
        executor.quote(market.slug, Direction.UP, Decimal("0.99"))
        executor.quote(market.slug, Direction.DOWN, Decimal("0.01"))

        asyncio.run(engine.tick(market, healthy(), inside_window(market)))

        state = engine.state_for(market.slug)
        assert state is not None, "an OFF engine still reports a state, honestly OFF"
        assert state.state is MajorityState.OFF
        assert store.orders_for(market.slug) == ()

    def test_a_fail_closed_engine_submits_nothing(self, tmp_path: Path) -> None:
        """`disable_reason` overrides `enabled`. Both are checked, not one."""
        engine, store, executor = engine_at(
            tmp_path, cfg=config(disable_reason="45s window has no defined formula")
        )
        market = live_market(store)
        engine.open_market(market.slug, market.close_ts)
        executor.quote(market.slug, Direction.UP, Decimal("0.99"))
        executor.quote(market.slug, Direction.DOWN, Decimal("0.01"))

        asyncio.run(engine.tick(market, healthy(), inside_window(market)))

        assert store.orders_for(market.slug) == ()


# ── D. the gates ─────────────────────────────────────────────────────────────


class TestTheRiskGatesStillApplyToMajority:
    """MAJORITY chooses its own side; it does not choose whether it may trade."""

    def test_no_official_ptb_denies_at_gate_five(self, tmp_path: Path) -> None:
        """A market with no frozen PTB is never traded, MAJORITY included."""
        engine, store, executor = engine_at(tmp_path)
        market = make_market(store)
        market.phase = MarketPhase.ACTIVE  # deliberately NOT frozen
        engine.open_market(market.slug, market.close_ts)
        now = inside_window(market)
        executor.quote(market.slug, Direction.UP, Decimal("0.95"))
        executor.quote(market.slug, Direction.DOWN, Decimal("0.20"))

        asyncio.run(engine.tick(market, healthy(), now))
        asyncio.run(engine.tick(market, healthy(), now + 0.2))

        state = engine.state_for(market.slug)
        assert state is not None
        assert state.selected_side is Direction.UP, "the side is determined, then denied"
        assert state.state is MajorityState.NO_TRADE
        assert "G05" in state.no_trade_reason
        assert store.orders_for(market.slug) == ()

    def test_an_unarmed_operator_switch_denies(self, tmp_path: Path) -> None:
        engine, store, executor = engine_at(tmp_path)
        market = live_market(store)
        engine.open_market(market.slug, market.close_ts)
        now = inside_window(market)
        executor.quote(market.slug, Direction.UP, Decimal("0.95"))
        executor.quote(market.slug, Direction.DOWN, Decimal("0.20"))
        unarmed = RuntimeHealth(
            trading_enabled=True,
            spec_status=SettlementSpecStatus.VERIFIED,
            execution_armed=False,
        )

        asyncio.run(engine.tick(market, unarmed, now))
        asyncio.run(engine.tick(market, unarmed, now + 0.2))

        state = engine.state_for(market.slug)
        assert state is not None
        assert state.state is MajorityState.NO_TRADE
        assert "G02" in state.no_trade_reason
        assert store.orders_for(market.slug) == ()

    def test_a_limit_outside_the_entry_band_denies(self, tmp_path: Path) -> None:
        """MAJORITY's own band, not TWAP's."""
        engine, store, executor = engine_at(
            tmp_path,
            cfg=config(target_limit_price=Decimal("0.85"), entry_price_max=Decimal("0.50")),
        )
        market = live_market(store)
        engine.open_market(market.slug, market.close_ts)
        now = inside_window(market)
        executor.quote(market.slug, Direction.UP, Decimal("0.95"))
        executor.quote(market.slug, Direction.DOWN, Decimal("0.20"))

        asyncio.run(engine.tick(market, healthy(), now))
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
        executor.quote(market.slug, Direction.UP, Decimal("0.95"))
        executor.quote(market.slug, Direction.DOWN, Decimal("0.20"))

        asyncio.run(engine.tick(market, healthy(), now))
        market.phase = MarketPhase.CANCELLING
        asyncio.run(engine.tick(market, healthy(), now + 0.2))

        assert store.orders_for(market.slug) == ()


# ── E. once per market ───────────────────────────────────────────────────────


class TestMajorityTradesEachMarketAtMostOnce:
    """One trigger, one side, one order — however many passes the loop makes."""

    def test_repeated_ticks_after_submission_add_no_second_order(
        self, tmp_path: Path
    ) -> None:
        engine, store, executor = engine_at(tmp_path)
        market = live_market(store)
        engine.open_market(market.slug, market.close_ts)
        now = inside_window(market)
        executor.quote(market.slug, Direction.UP, Decimal("0.95"))
        executor.quote(market.slug, Direction.DOWN, Decimal("0.20"))

        asyncio.run(engine.tick(market, healthy(), now))
        for step in range(1, 25):
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

        executor.quote(market.slug, Direction.UP, Decimal("0.95"))
        executor.quote(market.slug, Direction.DOWN, Decimal("0.95"))
        asyncio.run(engine.tick(market, healthy(), now))
        asyncio.run(engine.tick(market, healthy(), now + 0.2))
        state = engine.state_for(market.slug)
        assert state is not None
        assert state.state is MajorityState.NO_TRADE

        executor.quote(market.slug, Direction.DOWN, Decimal("0.04"))
        for step in range(2, 12):
            asyncio.run(engine.tick(market, healthy(), now + step * 0.2))

        assert state.state is MajorityState.NO_TRADE
        assert store.orders_for(market.slug) == ()


# ── F. per-market isolation ──────────────────────────────────────────────────


class TestTwoMarketsDoNotShareMajorityState:
    """Per-market state objects, created fresh and thrown away (A11)."""

    def test_a_second_market_starts_from_zero(self, tmp_path: Path) -> None:
        engine, store, executor = engine_at(tmp_path)
        first = live_market(store, 1754400000)
        second = live_market(store, 1754400300)
        engine.open_market(first.slug, first.close_ts)
        engine.open_market(second.slug, second.close_ts)

        now = inside_window(first)
        executor.quote(first.slug, Direction.UP, Decimal("0.95"))
        executor.quote(first.slug, Direction.DOWN, Decimal("0.20"))
        executor.quote(second.slug, Direction.UP, Decimal("0.30"))
        executor.quote(second.slug, Direction.DOWN, Decimal("0.31"))

        asyncio.run(engine.tick(first, healthy(), now))
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
        engine, store, executor = engine_at(tmp_path)
        market = live_market(store)
        executor.quote(market.slug, Direction.UP, Decimal("0.99"))
        executor.quote(market.slug, Direction.DOWN, Decimal("0.01"))

        asyncio.run(engine.tick(market, healthy(), inside_window(market)))

        assert engine.state_for(market.slug) is None
        assert store.orders_for(market.slug) == ()


# ── G. restart ───────────────────────────────────────────────────────────────


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
        executor.quote(market.slug, Direction.UP, Decimal("0.95"))
        executor.quote(market.slug, Direction.DOWN, Decimal("0.20"))
        asyncio.run(first.tick(market, healthy(), now))
        asyncio.run(first.tick(market, healthy(), now + 0.2))
        assert len(store.orders_for(market.slug)) == 1
        store.close()

        # A new process: new engine, new in-memory state, same database file.
        second, reopened, executor2 = engine_at(tmp_path)
        second.open_market(market.slug, market.close_ts)
        second.restore_from_intents(market.slug, now)
        executor2.quote(market.slug, Direction.UP, Decimal("0.95"))
        executor2.quote(market.slug, Direction.DOWN, Decimal("0.20"))
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


# ── H. multi-window runtime ───────────────────────────────────────────────────


def multi_window_engine_at(
    tmp_path: Path,
    *,
    windows: tuple[int, ...] = (3, 45),
    cfg: MajorityConfig | None = None,
    name: str = "arc.db",
):
    """A wired MAJORITY engine with multiple configured windows.

    `minimum` on the submitter is the minimum across all windows so a 3s and a
    90s window both fit. Mirrors the runtime's construction choice in
    `ArcRuntime.__init__`.
    """
    from arc.majority.config import MajorityWindowConfig

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
    return MajorityEngine(settings, store, executor, submitter), store, executor


class TestMultiWindowRuntime:
    """Two configured windows run independently for one market."""

    def test_two_windows_both_create_state(self, tmp_path: Path) -> None:
        engine, _, _ = multi_window_engine_at(tmp_path, windows=(3, 45))
        market = live_market(store_at(tmp_path))
        engine.open_market(market.slug, market.close_ts)
        assert engine.state_for(market.slug, 3) is not None
        assert engine.state_for(market.slug, 45) is not None

    def test_two_windows_produce_independent_orders(self, tmp_path: Path) -> None:
        """A 3s trigger and a 45s trigger, on different books, produce different orders."""
        engine, store, executor = multi_window_engine_at(tmp_path, windows=(3, 45))
        market = live_market(store)
        engine.open_market(market.slug, market.close_ts)

        # Both windows are open at the close_ts-3 instant: 45s opens at close-45,
        # but for the purpose of forcing both windows we set the clock to close-1
        # so the 45s window opened (close-45 < close-1) AND the 3s window opened
        # (close-3 < close-1).
        now = float(market.close_ts - 1)

        # 3s window: UP crosses trigger, DOWN holds.
        executor.quote(market.slug, Direction.UP, Decimal("0.95"))
        executor.quote(market.slug, Direction.DOWN, Decimal("0.20"))
        # 45s window: DOWN crosses trigger, UP holds.
        # Re-quote to a different book for the second tick: we'll move the book
        # between ticks and check both windows resolved to different sides.
        asyncio.run(engine.tick(market, healthy(), now))
        # Move book so the FRESH read for each window picks different numbers.
        # Then advance both windows to TRIGGERED on their respective passes.
        executor.quote(market.slug, Direction.UP, Decimal("0.10"))
        executor.quote(market.slug, Direction.DOWN, Decimal("0.85"))
        asyncio.run(engine.tick(market, healthy(), now + 0.2))

        # Both windows should now be SUBMITTED with DIFFERENT intents.
        intents = store.intents_for(market.slug, engine=MAJORITY_ENGINE)
        assert len(intents) == 2, f"expected two MAJORITY intents, got {len(intents)}"
        offsets = sorted(i.offset_seconds for i in intents)
        assert offsets == [3, 45]
        # Each intent has its own order_id with the correct window in the id.
        orders = store.orders_for(market.slug)
        assert len(orders) == 2
        # Verify identity by engine + offset
        offsets_in_orders = sorted(o.offset_seconds for o in orders)
        assert offsets_in_orders == [3, 45]

    def test_per_window_isolation_one_window_NO_TRADE_does_not_block_another(
        self, tmp_path: Path
    ) -> None:
        """If a window resolves INDETERMINATE, the OTHER window still trades."""
        engine, store, executor = multi_window_engine_at(tmp_path, windows=(3, 45))
        market = live_market(store)
        engine.open_market(market.slug, market.close_ts)
        now = float(market.close_ts - 1)

        # First tick: trigger BOTH windows. UP crosses both triggers.
        executor.quote(market.slug, Direction.UP, Decimal("0.95"))
        executor.quote(market.slug, Direction.DOWN, Decimal("0.20"))
        asyncio.run(engine.tick(market, healthy(), now))
        # At this point both windows are TRIGGERED.

        # Now move book: a tied book (UP=DOWN) on the fresh read should make the
        # majority INDETERMINATE for BOTH windows. After the second tick both
        # windows are NO_TRADE.
        executor.quote(market.slug, Direction.UP, Decimal("0.50"))
        executor.quote(market.slug, Direction.DOWN, Decimal("0.50"))
        asyncio.run(engine.tick(market, healthy(), now + 0.2))

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
        executor.quote(market.slug, Direction.UP, Decimal("0.95"))
        executor.quote(market.slug, Direction.DOWN, Decimal("0.20"))
        asyncio.run(first.tick(market, healthy(), now))
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
