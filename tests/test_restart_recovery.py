"""Restart recovery. The process dies; the venue does not.

Every test here reopens the SAME SQLite file with a new Store, against a venue
object that survived — which is exactly what a PM2 restart or a VPS reboot looks
like from the outside. The property under test is that nothing is submitted twice,
nothing resting is forgotten, and trading does not resume while anything at the
venue is unaccounted for.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal
from pathlib import Path

import pytest
from execution_fixtures import (
    LIMIT_PRICE,
    WINDOW_TS,
    fill_engine,
    intent_for,
    make_market,
    reconciler,
    store_at,
    submitter,
)

from arc.domain.enums import Direction, MarketPhase, OrderState
from arc.execution.orders import new_order, order_id_for, transition
from arc.execution.v1_paper import PaperExecutor
from arc.runtime.recovery import RecoveryRunner, RecoveryStep, markets_needing_sweep
from arc.storage.store import Store

NOW = float(WINDOW_TS + 297)
SLUG_OFFSET = 3


def _runner(store: Store, executor: PaperExecutor) -> RecoveryRunner:
    return RecoveryRunner(store, reconciler(store, executor), fill_engine(store, executor))


def _recover(store: Store, executor: PaperExecutor, **hooks: object):  # type: ignore[no-untyped-def]
    return asyncio.run(_runner(store, executor).run(NOW, **hooks))  # type: ignore[arg-type]


def _submit(store: Store, executor: PaperExecutor, count: int = 1):  # type: ignore[no-untyped-def]
    return asyncio.run(
        submitter(store, executor).submit(
            intent_for(), count=count, phase=MarketPhase.ACTIVE, now=NOW
        )
    )


@pytest.fixture
def running(tmp_path: Path):  # type: ignore[no-untyped-def]
    """A process mid-market: one market, one venue, orders resting."""
    store = store_at(tmp_path)
    market = make_market(store)
    executor = PaperExecutor()
    yield store, executor, market, tmp_path
    store.close()


def _restart(store: Store, tmp_path: Path) -> Store:
    """Close the process's handle and come back up on the same database file."""
    store.close()
    return store_at(tmp_path)


class TestTheSequenceIsFixed:
    def test_the_steps_run_in_the_contracted_order(self, running) -> None:  # type: ignore[no-untyped-def]
        """Reconnect -> feeds -> websocket -> wallet -> orders -> positions ->
        pending -> windows. Resuming windows before the book is known is what
        produces a second submission for a window that already has one."""
        store, executor, _market, _tmp = running
        report = _recover(store, executor)

        assert [s.step for s in report.steps] == [
            RecoveryStep.RECONNECT,
            RecoveryStep.FEEDS,
            RecoveryStep.WEBSOCKET,
            RecoveryStep.WALLET,
            RecoveryStep.LIVE_ORDERS,
            RecoveryStep.POSITIONS,
            RecoveryStep.PENDING_ORDERS,
            RecoveryStep.WINDOWS,
        ]

    def test_the_hooks_fire_in_that_same_order(self, running) -> None:  # type: ignore[no-untyped-def]
        store, executor, _market, _tmp = running
        calls: list[str] = []

        async def note(name: str) -> None:
            calls.append(name)

        async def windows() -> tuple[str, ...]:
            calls.append("windows")
            return ()

        _recover(
            store,
            executor,
            reconnect=lambda: note("reconnect"),
            feeds=lambda: note("feeds"),
            websocket=lambda: note("websocket"),
            wallet=lambda: note("wallet"),
            windows=windows,
        )
        assert calls == ["reconnect", "feeds", "websocket", "wallet", "windows"]

    def test_an_unconfigured_hook_is_a_pass_not_a_failure(self, running) -> None:  # type: ignore[no-untyped-def]
        store, executor, _market, _tmp = running
        report = _recover(store, executor)
        assert report.ok and report.safe_to_trade

    def test_a_failing_hook_is_recorded_and_the_sequence_continues(self, running) -> None:  # type: ignore[no-untyped-def]
        """A step that raised out of recovery would take down the process that is
        trying to recover."""
        store, executor, _market, _tmp = running

        async def broken() -> None:
            raise OSError("no route to host")

        report = _recover(store, executor, feeds=broken)

        assert len(report.steps) == 8
        assert not report.ok
        assert not report.safe_to_trade
        failed = next(s for s in report.steps if s.step is RecoveryStep.FEEDS)
        assert "no route to host" in failed.detail


class TestNoDuplicateSubmissions:
    def test_replaying_a_submission_after_restart_places_no_second_order(
        self, running
    ) -> None:  # type: ignore[no-untyped-def]
        store, executor, market, tmp_path = running
        first = _submit(store, executor, count=3)
        assert len(asyncio.run(executor.open_orders(market.slug))) == 3

        store = _restart(store, tmp_path)
        _recover(store, executor)
        second = _submit(store, executor, count=3)

        assert [o.order_id for o in second] == [o.order_id for o in first]
        assert len(asyncio.run(executor.open_orders(market.slug))) == 3
        assert len(store.orders_for(market.slug)) == 3
        store.close()

    def test_the_replayed_orders_keep_their_venue_ids(self, running) -> None:  # type: ignore[no-untyped-def]
        """A row that came back without its venue id could never be cancelled."""
        store, executor, _market, tmp_path = running
        before = {o.order_id: o.venue_order_id for o in _submit(store, executor, count=2)}

        store = _restart(store, tmp_path)
        after = {o.order_id: o.venue_order_id for o in _submit(store, executor, count=2)}

        assert after == before
        assert all(after.values())
        store.close()

    def test_order_ids_are_identical_across_two_independent_runs(
        self, tmp_path: Path
    ) -> None:
        """Derived ids, not generated ones. A uuid would double the position."""
        ids: list[tuple[str, ...]] = []
        for name in ("a.db", "b.db"):
            store = store_at(tmp_path, name)
            make_market(store)
            ids.append(tuple(o.order_id for o in _submit(store, PaperExecutor(), 3)))
            store.close()
        assert ids[0] == ids[1]


class TestPendingRowsAreResolved:
    def test_an_unsubmitted_row_is_expired_at_restart(self, running) -> None:  # type: ignore[no-untyped-def]
        """The process died between the write and the venue call (A4)."""
        store, executor, market, tmp_path = running
        order = new_order(
            market_slug=market.slug,
            offset_seconds=SLUG_OFFSET,
            index=0,
            generation=0,
            direction=Direction.UP,
            price=LIMIT_PRICE,
            size=Decimal("35"),
            now=NOW,
        )
        store.save_order(order)
        assert store.orders_for(market.slug)[0].state is OrderState.PENDING

        store = _restart(store, tmp_path)
        report = _recover(store, executor)

        assert store.orders_for(market.slug)[0].state is OrderState.EXPIRED
        pending = next(s for s in report.steps if s.step is RecoveryStep.PENDING_ORDERS)
        assert pending.detail == "1 expired"
        store.close()

    def test_an_expired_row_is_not_resubmitted_by_a_later_replay(self, running) -> None:  # type: ignore[no-untyped-def]
        """It carries a price frozen before the restart; the window has moved on."""
        store, executor, market, tmp_path = running
        store.save_order(
            new_order(
                market_slug=market.slug,
                offset_seconds=SLUG_OFFSET,
                index=0,
                generation=0,
                direction=Direction.UP,
                price=LIMIT_PRICE,
                size=Decimal("35"),
                now=NOW,
            )
        )
        store = _restart(store, tmp_path)
        _recover(store, executor)

        replayed = _submit(store, executor, count=1)

        assert replayed[0].state is OrderState.EXPIRED
        assert asyncio.run(executor.open_orders(market.slug)) == ()
        store.close()


class TestTradingIsGatedUntilTheBookIsKnown:
    def test_an_unknown_order_still_resting_blocks_trading(self, running) -> None:  # type: ignore[no-untyped-def]
        store, executor, _market, tmp_path = running
        orders = _submit(store, executor, count=1)
        transition(orders[0], OrderState.INDETERMINATE, NOW, "connection lost")
        store.save_order(orders[0])

        store = _restart(store, tmp_path)
        report = _recover(store, executor)

        # Reconciliation found it resting, so it is resolved as live — and live
        # means new submissions would stack on top of it.
        assert report.unresolved_orders == (orders[0].order_id,)
        assert not report.safe_to_trade
        store.close()

    def test_an_unknown_order_absent_from_the_venue_is_closed_out(self, running) -> None:  # type: ignore[no-untyped-def]
        store, executor, market, tmp_path = running
        orders = _submit(store, executor, count=1)
        asyncio.run(executor.cancel(orders[0]))
        transition(orders[0], OrderState.INDETERMINATE, NOW, "connection lost")
        store.save_order(orders[0])

        store = _restart(store, tmp_path)
        report = _recover(store, executor)

        assert report.unresolved_orders == ()
        assert report.safe_to_trade
        assert store.orders_for(market.slug)[0].state is OrderState.CANCELLED
        store.close()

    def test_an_unreachable_venue_does_not_read_as_an_empty_book(self, running) -> None:  # type: ignore[no-untyped-def]
        """Treating a failed reconcile as "nothing resting" is the double-fill path."""
        store, executor, _market, tmp_path = running
        orders = _submit(store, executor, count=2)

        class Unreachable(PaperExecutor):
            async def open_orders(self, market_slug: str):  # type: ignore[no-untyped-def]
                raise OSError("connection reset")

        store = _restart(store, tmp_path)
        report = _recover(store, Unreachable())

        assert set(report.unresolved_orders) == {o.order_id for o in orders}
        assert not report.safe_to_trade
        store.close()

    def test_an_order_at_the_venue_with_no_local_row_is_an_orphan(self, running) -> None:  # type: ignore[no-untyped-def]
        """It cannot be cancelled blind — on a shared account it may not be ARC's."""
        store, executor, market, _tmp_path = running
        stranger = new_order(
            market_slug=market.slug,
            offset_seconds=SLUG_OFFSET,
            index=9,
            generation=0,
            direction=Direction.UP,
            price=LIMIT_PRICE,
            size=Decimal("5"),
            now=NOW,
        )
        asyncio.run(executor.place(stranger))

        report = _recover(store, executor)

        assert report.orphans == (order_id_for(market.slug, SLUG_OFFSET, 9, 0),)
        assert not report.ok
        assert not report.safe_to_trade


class TestRecoveryIsIdempotent:
    def test_running_it_twice_converges_on_the_same_state(self, running) -> None:  # type: ignore[no-untyped-def]
        """A VPS that reboots twice in a minute runs it twice."""
        store, executor, market, _tmp_path = running
        _submit(store, executor, count=2)
        executor.trade(market.slug, LIMIT_PRICE, Decimal("18"))
        asyncio.run(fill_engine(store, executor).poll(market.slug, NOW))

        first = _recover(store, executor)
        snapshot = {o.order_id: (o.state, o.filled_size) for o in store.orders_for(market.slug)}
        second = _recover(store, executor)

        assert [s.step for s in first.steps] == [s.step for s in second.steps]
        assert {
            o.order_id: (o.state, o.filled_size) for o in store.orders_for(market.slug)
        } == snapshot

    def test_fills_recorded_before_the_crash_are_not_re_applied(self, running) -> None:  # type: ignore[no-untyped-def]
        store, executor, market, tmp_path = running
        _submit(store, executor, count=1)
        executor.trade(market.slug, LIMIT_PRICE, Decimal("20"))
        asyncio.run(fill_engine(store, executor).poll(market.slug, NOW))

        store = _restart(store, tmp_path)
        _recover(store, executor)
        asyncio.run(fill_engine(store, executor).poll(market.slug, NOW + 1))

        assert store.filled_size_for_window(market.slug, SLUG_OFFSET) == Decimal("20")
        assert len(store.fills_for(market.slug)) == 1
        store.close()


class TestOrdersLeftRestingAreFound:
    def test_a_market_with_live_orders_is_flagged_for_sweeping(self, running) -> None:  # type: ignore[no-untyped-def]
        store, executor, market, tmp_path = running
        _submit(store, executor, count=1)

        store = _restart(store, tmp_path)
        _recover(store, executor)

        assert markets_needing_sweep(store) == (market.slug,)
        store.close()

    def test_a_settled_market_is_not_flagged(self, running) -> None:  # type: ignore[no-untyped-def]
        store, executor, market, _tmp_path = running
        _submit(store, executor, count=1)
        store.save_phase(market.slug, MarketPhase.SETTLED, NOW)

        assert markets_needing_sweep(store) == ()

    def test_a_market_with_nothing_resting_is_not_flagged(self, running) -> None:  # type: ignore[no-untyped-def]
        store, _executor, _market, _tmp_path = running
        assert markets_needing_sweep(store) == ()
