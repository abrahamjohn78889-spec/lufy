"""The settlement writer: a closed market becomes a settlement row.

Production once archived every market one window late with NO settlement row at
all — nothing ever called save_settlement, so realised P&L, wins and losses could
never exist for the Ledger, the Bankroll or Analytics. These tests hold the fix:

  close → settle    a SETTLING market whose 30-second settlement window has been
                    fully observed is written out once the grace period passes,
                    with the correct outcome, cost-valued P&L, per-engine row,
                    archive and per-market cleanup.
  honesty           before the grace period, or with an incomplete window,
                    nothing is written. A missing number postpones settlement;
                    it never becomes an invented one.
  equality          TWAP == PTB resolves DOWN — the same strict comparison the
                    entry side uses, so a position on UP loses that tie.
  recovery          a market a previous process closed but never wrote out is
                    settled at startup from the persisted observations, with no
                    live rotator state at all.
"""

from __future__ import annotations

import io
from decimal import Decimal
from typing import Any

from conftest import VALID_TRADING_VALUES

from arc.clock import FrozenClock
from arc.config import ArcSettings, Settings, build_trading_config
from arc.domain.enums import Direction, MarketPhase, OrderState, Outcome
from arc.domain.models import Fill, Observation, Order
from arc.execution.v1_paper import PaperExecutor
from arc.majority.config import MAJORITY_ENGINE
from arc.market.discovery import build_discovery
from arc.market.feed import RtdsFeed
from arc.market.settlement_feed import SettlementTwapCollector
from arc.runtime.engine import ArcRuntime
from arc.runtime.state import RuntimeState
from arc.storage.store import Store

_NOW = 1_754_400_000.0  # exactly on a 300-second window grid


def _settings() -> Settings:
    return Settings(
        env=ArcSettings(_env_file=None),
        trading=build_trading_config(dict(VALID_TRADING_VALUES)),
        seeded_from_env=False,
    )


def _runtime(db_path: str) -> tuple[ArcRuntime, Store, FrozenClock]:
    """A directly constructed paper runtime, like the lifecycle tests build it."""
    store = Store(db_path)
    store.migrate(_NOW)
    clock = FrozenClock(_NOW)
    runtime = RuntimeState(store, clock)
    runtime.load()
    run = ArcRuntime(
        settings=_settings(),
        store=store,
        clock=clock,
        runtime=runtime,
        discovery=build_discovery(),
        feed=RtdsFeed(clock),
        executor=PaperExecutor(),
        out=io.StringIO(),
    )
    return run, store, clock


def _position(store: Store, slug: str, direction: Direction) -> None:
    """One filled MAJORITY order: 20 shares at 0.55."""
    order = Order(
        order_id=f"MAJORITY:{slug}:1",
        market_slug=slug,
        offset_seconds=30,
        direction=direction,
        price=Decimal("0.55"),
        size=Decimal("20"),
        state=OrderState.FILLED,
        created_at=_NOW + 280,
        engine=MAJORITY_ENGINE,
    )
    store.save_order(order)
    store.save_fill(
        Fill(
            fill_id="fill-1",
            order_id=order.order_id,
            market_slug=slug,
            size=Decimal("20"),
            price=Decimal("0.55"),
            ts=_NOW + 281,
            engine=MAJORITY_ENGINE,
        )
    )


def _open_then_close(
    run: ArcRuntime, clock: FrozenClock, direction: Direction = Direction.UP
) -> str:
    """Open market A with a frozen PTB, then advance across the boundary so A is
    SETTLING and B is current. Returns A's slug. Mirrors what the main loop does
    for the settlement collector, which in production is created on the opened
    event."""
    run.rotator.advance(_NOW + 1)
    market = run.rotator.current
    assert market is not None
    market.freeze_ptb("110000")
    # The production freeze persists the PTB (ptb.freeze_ptb_for); recovery reads
    # it back from this row.
    assert market.ptb is not None
    run._store.save_ptb(market.slug, market.ptb, clock.now())
    run._settlement[market.slug] = SettlementTwapCollector(
        market_slug=market.slug, close_ts=market.close_ts
    )
    _position(run._store, market.slug, direction)
    clock.set(_NOW + 301)
    run.rotator.advance(clock.now())
    assert market.phase is MarketPhase.SETTLING
    return market.slug


def _fill_settlement_window(run: ArcRuntime, slug: str, price: Decimal) -> None:
    """Offer samples covering the venue's inclusive [close - 30, close] window."""
    collector = run._settlement[slug]
    for ts in (collector.close_ts - 30, collector.close_ts - 20, collector.close_ts - 10,
               collector.close_ts):
        collector.offer(Observation(ts=ts, price=price))


def test_a_closed_market_settles_once_the_grace_period_passes(tmp_path: Any) -> None:
    run, store, clock = _runtime(f"{tmp_path}/arc.db")
    slug = _open_then_close(run, clock)
    _fill_settlement_window(run, slug, Decimal("110100"))  # above the 110000 PTB

    # Still inside the grace period: the loop may already see the SETTLING market,
    # but nothing may be written yet.
    run._settle_markets(clock.now())
    assert store.settlements_for(slug) == ()

    clock.set(_NOW + 300 + 6)  # close + 5s grace + 1
    run._settle_markets(clock.now())

    rows = store.settlements_for(slug)
    assert len(rows) == 1
    row = rows[0]
    assert row.engine == MAJORITY_ENGINE
    assert row.outcome is Outcome.UP
    assert row.settlement_twap == Decimal("110100")
    assert row.ptb == Decimal("110000")
    # Cost valuation: the winner pays out one per share. 20 x (1 - 0.55).
    assert row.pnl == Decimal("9.0")
    # Archived and cleaned up: no per-market state survives in the runtime.
    assert run.rotator.closing is None
    assert slug not in run._settlement
    assert str(store.load_market_row(slug)["phase"]) == MarketPhase.SETTLED.value


def test_a_losing_position_is_a_negative_pnl_row(tmp_path: Any) -> None:
    run, store, clock = _runtime(f"{tmp_path}/arc.db")
    slug = _open_then_close(run, clock, direction=Direction.DOWN)
    _fill_settlement_window(run, slug, Decimal("110100"))  # resolves UP

    clock.set(_NOW + 306)
    run._settle_markets(clock.now())

    rows = store.settlements_for(slug)
    assert len(rows) == 1
    assert rows[0].outcome is Outcome.UP
    # The loser paid its entry cost. -20 x 0.55.
    assert rows[0].pnl == Decimal("-11.0")


def test_twap_equal_to_ptb_resolves_down(tmp_path: Any) -> None:
    run, store, clock = _runtime(f"{tmp_path}/arc.db")
    slug = _open_then_close(run, clock)
    _fill_settlement_window(run, slug, Decimal("110000"))  # exactly the PTB

    clock.set(_NOW + 306)
    run._settle_markets(clock.now())

    rows = store.settlements_for(slug)
    assert len(rows) == 1
    # Strict comparison: equality is NOT up, so the UP position loses.
    assert rows[0].outcome is Outcome.DOWN
    assert rows[0].pnl == Decimal("-11.0")


def test_an_incomplete_settlement_window_writes_nothing(tmp_path: Any) -> None:
    run, store, clock = _runtime(f"{tmp_path}/arc.db")
    slug = _open_then_close(run, clock)
    # No samples at all: the collector cannot produce a TWAP.

    clock.set(_NOW + 306)
    run._settle_markets(clock.now())

    assert store.settlements_for(slug) == ()
    # The market is still SETTLING and still eligible on a later pass.
    assert slug in run._settlement


def test_restart_settles_a_market_the_previous_process_left_settling(
    tmp_path: Any,
) -> None:
    db = f"{tmp_path}/arc.db"
    run_a, store_a, clock_a = _runtime(db)
    slug = _open_then_close(run_a, clock_a)
    close_ts = _NOW + 300
    # The observations the collector held in memory are the persisted ones.
    store_a.save_observations(
        slug,
        [Observation(ts=ts, price=Decimal("110100")) for ts in
         (close_ts - 30, close_ts - 20, close_ts - 10, close_ts)],
        received_at=close_ts,
    )

    # A fresh process over the same store: no rotator state, no collectors.
    run_b, store_b, _ = _runtime(db)
    run_b._settle_recovered(_NOW + 700)

    rows = store_b.settlements_for(slug)
    assert len(rows) == 1
    assert rows[0].engine == MAJORITY_ENGINE
    assert rows[0].outcome is Outcome.UP
    assert rows[0].pnl == Decimal("9.0")
    assert rows[0].settled_at == _NOW + 700
    assert str(store_b.load_market_row(slug)["phase"]) == MarketPhase.SETTLED.value
    # The neighbour market that opened next door is ACTIVE, not settlement
    # material: recovery must leave it alone.
    next_slug = run_a.rotator.current.slug if run_a.rotator.current else ""
    if next_slug:
        assert store_b.settlements_for(next_slug) == ()
