"""The paper bankroll: one accounting system, read from the engines' rows.

The paper account is not a second ledger. It reads settlement, fill and order
rows the engines already wrote and exposes them as the operator's view of their
paper funds. These tests hold the contract:

  defaults        a fresh account starts at 100 with nothing in flight.
  open position   an unsettled filled order is committed cost; unrealised is
                  UNAVAILABLE when there is no quote, and marked against the
                  book once a quote exists.
  settlement      realised P&L updates balance/wins/losses once the market
                  settles; committed returns to zero.
  reset           moves the epoch forward so realised counts restart from
                  zero without deleting any settlement row.
  validation      set_start_balance refuses non-positive and non-decimal input.
  route guard     editing/resetting while the runtime is running is refused.
  skipped         windows that ended EXPIRED or NO_DIRECTION after the epoch
                  are counted via the store helper.
"""

from __future__ import annotations

import asyncio
import io
from decimal import Decimal
from typing import Any

import pytest
from conftest import VALID_TRADING_VALUES
from fastapi.testclient import TestClient

from arc.api.app import build_app
from arc.clock import FrozenClock
from arc.config import ArcSettings, Settings, build_trading_config
from arc.domain.enums import Direction, MarketPhase, OrderState, WindowState
from arc.domain.models import Fill, Observation, Order
from arc.errors import ArcError
from arc.execution.v1_paper import PaperExecutor
from arc.majority.config import MAJORITY_ENGINE
from arc.market.discovery import build_discovery
from arc.market.feed import RtdsFeed
from arc.market.settlement_feed import SettlementTwapCollector
from arc.runtime.engine import ArcRuntime, RuntimeStatus
from arc.runtime.paper_account import (
    KEY_PAPER_START,
    paper_account,
    reset_paper_account,
    set_start_balance,
)
from arc.runtime.state import RuntimeState
from arc.storage.store import Store

_NOW = 1_754_400_000.0


def _runtime(db_path: str) -> tuple[ArcRuntime, Store, FrozenClock]:
    store = Store(db_path)
    store.migrate(_NOW)
    clock = FrozenClock(_NOW)
    runtime = RuntimeState(store, clock)
    runtime.load()
    run = ArcRuntime(
        settings=Settings(
            env=ArcSettings(_env_file=None),
            trading=build_trading_config(dict(VALID_TRADING_VALUES)),
            seeded_from_env=False,
        ),
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
    """Open market A, advance past close_ts so it becomes SETTLING. Returns slug."""
    run.rotator.advance(_NOW + 1)
    market = run.rotator.current
    assert market is not None
    market.freeze_ptb("110000")
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
    collector = run._settlement[slug]
    for ts in (
        collector.close_ts - 30,
        collector.close_ts - 20,
        collector.close_ts - 10,
        collector.close_ts,
    ):
        collector.offer(Observation(ts=ts, price=price))


def test_defaults_for_a_fresh_account(tmp_path: Any) -> None:
    run, _, _ = _runtime(f"{tmp_path}/arc.db")
    block = asyncio.run(paper_account(run))
    assert block["start_balance"] == "100"
    assert block["epoch"] == 0.0
    assert block["balance"] == "100"
    assert block["available"] == "100"
    assert block["committed"] == "0"
    assert block["realized_pnl"] == "0"
    assert block["unrealized_pnl"] == "0"
    assert block["total_trades"] == 0
    assert block["wins"] == 0
    assert block["losses"] == 0
    assert block["skipped"] == 0
    assert block["open_trades"] == 0
    assert block["editable"] is True  # STOPPED by construction


def test_an_open_position_counts_as_committed_cost(tmp_path: Any) -> None:
    run, _store, clock = _runtime(f"{tmp_path}/arc.db")
    slug = _open_then_close(run, clock)
    # The market is now SETTLING — still in `unsettled_markets`, so the position
    # counts as committed until the settlement writer produces a row.
    block = asyncio.run(paper_account(run))
    assert block["committed"] == "11.00"  # 20 x 0.55
    assert block["available"] == "89.00"
    assert block["open_trades"] == 1
    # No quote yet → unrealised is unavailable, never invented as zero.
    assert block["unrealized_pnl"] is None

    # Once quoted, mark against the book. value_now = 20x0.60=12; cost=11; unrealised=1.
    assert isinstance(run.executor, PaperExecutor)
    run.executor.quote(slug, Direction.UP, Decimal("0.60"))
    block = asyncio.run(paper_account(run))
    assert block["unrealized_pnl"] == "1.00"


def test_realised_pnl_updates_after_settlement(tmp_path: Any) -> None:
    run, store, clock = _runtime(f"{tmp_path}/arc.db")
    slug = _open_then_close(run, clock)
    _fill_settlement_window(run, slug, Decimal("110100"))
    clock.set(_NOW + 306)
    run._settle_markets(clock.now())
    # Settlement row must exist for the assertion below to mean anything.
    assert len(store.settlements_for(slug)) == 1

    block = asyncio.run(paper_account(run))
    assert block["realized_pnl"] == "9.00"
    assert block["balance"] == "109.00"
    assert block["wins"] == 1
    assert block["total_trades"] == 1
    # Settled markets drop out of unsettled, so committed returns to zero.
    assert block["committed"] == "0"
    assert block["open_trades"] == 0


def test_reset_moves_the_epoch_without_deleting_history(tmp_path: Any) -> None:
    run, store, clock = _runtime(f"{tmp_path}/arc.db")
    slug = _open_then_close(run, clock)
    _fill_settlement_window(run, slug, Decimal("110100"))
    clock.set(_NOW + 306)
    run._settle_markets(clock.now())
    assert len(store.settlements_for(slug)) == 1

    reset_epoch = reset_paper_account(store, clock.now())
    block = asyncio.run(paper_account(run))
    assert block["epoch"] == reset_epoch
    assert block["balance"] == "100"
    assert block["realized_pnl"] == "0"
    assert block["wins"] == 0
    # History is intact: the Ledger can still render this settlement.
    assert len(store.settlements_for(slug)) == 1


def test_set_start_balance_validates_and_persists(tmp_path: Any) -> None:
    _run, store, _ = _runtime(f"{tmp_path}/arc.db")
    # Positive integer works.
    result = set_start_balance(store, "250", _NOW)
    assert result == Decimal("250")
    assert store.get_runtime_state(KEY_PAPER_START) == "250"
    # Decimal string works.
    set_start_balance(store, "123.45", _NOW)
    assert store.get_runtime_state(KEY_PAPER_START) == "123.45"
    # Rejects.
    with pytest.raises(ArcError):
        set_start_balance(store, "0", _NOW)
    with pytest.raises(ArcError):
        set_start_balance(store, "-5", _NOW)
    with pytest.raises(ArcError):
        set_start_balance(store, "not-a-number", _NOW)
    # The persisted value is unchanged by the rejected attempts.
    assert store.get_runtime_state(KEY_PAPER_START) == "123.45"


def test_route_refuses_edits_while_running(tmp_path: Any) -> None:
    run, _, _ = _runtime(f"{tmp_path}/arc.db")
    client = TestClient(build_app(run))
    # Running: both start-balance edit and reset are refused.
    run.status = RuntimeStatus.RUNNING_V1
    response = client.post(
        "/settings", params={"action": "paper"}, json={"start_balance": "200"}
    )
    assert response.status_code == 409
    response = client.post("/settings", params={"action": "paper"}, json={"reset": True})
    assert response.status_code == 409

    # Stopped: both succeed and return the updated paper block.
    run.status = RuntimeStatus.STOPPED
    response = client.post(
        "/settings", params={"action": "paper"}, json={"start_balance": "200"}
    )
    assert response.status_code == 200
    assert response.json()["paper"]["start_balance"] == "200"
    response = client.post("/settings", params={"action": "paper"}, json={"reset": True})
    assert response.status_code == 200
    assert response.json()["saved"] is True


def test_skipped_windows_since_epoch_are_counted(tmp_path: Any) -> None:
    run, store, clock = _runtime(f"{tmp_path}/arc.db")
    # Advance into a market so its window rows exist (PENDING by default).
    run.rotator.advance(_NOW + 1)
    slug = run.rotator.current.slug if run.rotator.current else ""
    assert slug
    # Repurpose two of the default windows: one FIRED (should NOT count) and one
    # EXPIRED (should count). save_window_state is the only public writer for
    # terminal window states; ExecutionWindow has no market_slug field because
    # the row's identity lives in the DB, not on the dataclass.
    store.save_window_state(slug, 5, WindowState.FIRED, fired_at=clock.now())
    store.save_window_state(slug, 10, WindowState.EXPIRED)

    block = asyncio.run(paper_account(run))
    assert block["skipped"] == 1

    # Reset to a future epoch: neither window is counted any more because the
    # market's window_ts (its open time) is before the new epoch.
    reset_paper_account(store, _NOW + 600)
    block = asyncio.run(paper_account(run))
    assert block["skipped"] == 0
