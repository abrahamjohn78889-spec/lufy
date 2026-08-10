"""Two engines, one database, one execution stack: proof they cannot reach each other.

TWAP and MAJORITY share the Store, the Submitter, the Sweeper, the Reconciler and
the order FSM. That sharing is deliberate — a second execution framework would be a
second set of crash-safety behaviour to keep correct — but it means every isolation
guarantee is a property of ONE implementation being engine-aware in the right places
and deliberately engine-BLIND in exactly one place.

The one place that must stay blind is the market-close safety sweep. An order still
resting when the market settles is an uncontrolled position regardless of which
engine placed it, so the close sweep cancels everything. Every other engine-scoped
operation must see only its own rows.

Nothing internal is mocked. The real Store, real Submitter, real Sweeper, real
Reconciler and real order FSM are used; the only substitute is the V1 paper adapter,
which is production code.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal
from pathlib import Path

from execution_fixtures import (
    LIMIT_PRICE,
    WINDOW_TS,
    intent_for,
    make_market,
    reconciler,
    store_at,
    submitter,
    sweeper,
)

from arc.domain.enums import DEFAULT_ENGINE, Direction, MarketPhase, OrderState, Outcome
from arc.domain.models import Fill, Order, Settlement
from arc.execution.orders import chain_id_for, new_order, next_generation_id, order_id_for
from arc.execution.v1_paper import PaperExecutor
from arc.majority.config import MAJORITY_ENGINE
from arc.storage.store import Store

MAJORITY_WINDOW = 45
TWAP_OFFSET = 3


# ── A. identity ──────────────────────────────────────────────────────────────


class TestExistingTwapIdentityIsUnchanged:
    """The frozen contract. These strings are asserted as LITERALS, never recomputed.

    A test that compared `order_id_for(...)` against `chain_id_for(...) + ":0"` would
    pass no matter what both of them returned. The literal is the point: it is what a
    pre-migration database already holds, and reconciliation matches the venue by
    exactly this string.
    """

    SLUG = "btc-updown-5m-1786263900"

    def test_chain_id_is_byte_identical(self) -> None:
        assert chain_id_for(self.SLUG, 3, 0) == "btc-updown-5m-1786263900:3:0"

    def test_order_id_is_byte_identical(self) -> None:
        assert order_id_for(self.SLUG, 3, 0, 0) == "btc-updown-5m-1786263900:3:0:0"

    def test_explicit_twap_engine_produces_the_same_string(self) -> None:
        """Passing the default explicitly must not add a prefix."""
        assert order_id_for(self.SLUG, 3, 0, 0, DEFAULT_ENGINE) == order_id_for(
            self.SLUG, 3, 0, 0
        )

    def test_twap_gets_no_prefix_at_any_generation(self) -> None:
        for generation in range(4):
            assert order_id_for(self.SLUG, 3, 0, generation).startswith(self.SLUG)

    def test_default_engine_is_twap(self) -> None:
        assert DEFAULT_ENGINE == "TWAP"


class TestTheTwoEnginesCannotCollide:
    """Case A: identical market, window, index and generation. Distinct ids."""

    SLUG = "btc-updown-5m-1786263900"

    def test_same_coordinates_yield_different_order_ids(self) -> None:
        twap = order_id_for(self.SLUG, MAJORITY_WINDOW, 0, 0)
        majority = order_id_for(self.SLUG, MAJORITY_WINDOW, 0, 0, MAJORITY_ENGINE)
        assert twap != majority

    def test_the_majority_id_is_prefixed(self) -> None:
        assert order_id_for(self.SLUG, MAJORITY_WINDOW, 0, 0, MAJORITY_ENGINE) == (
            "MAJORITY:btc-updown-5m-1786263900:45:0:0"
        )

    def test_same_coordinates_yield_different_chain_ids(self) -> None:
        assert chain_id_for(self.SLUG, MAJORITY_WINDOW, 0) != chain_id_for(
            self.SLUG, MAJORITY_WINDOW, 0, MAJORITY_ENGINE
        )

    def test_no_generation_of_one_engine_equals_any_of_the_other(self) -> None:
        """Exhaustive over the generations a reprice chain realistically reaches."""
        twap = {order_id_for(self.SLUG, MAJORITY_WINDOW, i, g)
                for i in range(3) for g in range(5)}
        majority = {order_id_for(self.SLUG, MAJORITY_WINDOW, i, g, MAJORITY_ENGINE)
                    for i in range(3) for g in range(5)}
        assert twap.isdisjoint(majority)


# ── B, C, H. reprice chains ──────────────────────────────────────────────────


class TestRepriceGenerationsAdvanceForBothEngines:
    """Cases B, C and H. The generation is the RIGHTMOST component for both engines.

    `next_generation_id` reads from the right with rpartition, which is what makes a
    left-hand engine prefix invisible to it. If it had read from the left instead, a
    repriced MAJORITY order would have taken an id belonging to the other engine's
    chain — and then escaped every engine-scoped sweep for the rest of the market.
    """

    SLUG = "btc-updown-5m-1786263900"

    def _order(self, engine: str, generation: int) -> Order:
        return new_order(
            market_slug=self.SLUG,
            offset_seconds=MAJORITY_WINDOW,
            index=0,
            generation=generation,
            direction=Direction.UP,
            price=LIMIT_PRICE,
            size=Decimal("10"),
            now=1.0,
            engine=engine,
        )

    def test_twap_advances_0_1_2(self) -> None:
        ids = [self._order(DEFAULT_ENGINE, 0).order_id]
        for _ in range(2):
            order = self._order(DEFAULT_ENGINE, 0)
            order.order_id = ids[-1]
            ids.append(next_generation_id(order))
        assert ids == [
            "btc-updown-5m-1786263900:45:0:0",
            "btc-updown-5m-1786263900:45:0:1",
            "btc-updown-5m-1786263900:45:0:2",
        ]

    def test_majority_advances_0_1_2(self) -> None:
        ids = [self._order(MAJORITY_ENGINE, 0).order_id]
        for _ in range(2):
            order = self._order(MAJORITY_ENGINE, 0)
            order.order_id = ids[-1]
            ids.append(next_generation_id(order))
        assert ids == [
            "MAJORITY:btc-updown-5m-1786263900:45:0:0",
            "MAJORITY:btc-updown-5m-1786263900:45:0:1",
            "MAJORITY:btc-updown-5m-1786263900:45:0:2",
        ]

    def test_the_majority_prefix_survives_every_generation(self) -> None:
        order = self._order(MAJORITY_ENGINE, 0)
        for _ in range(5):
            order.order_id = next_generation_id(order)
            assert order.order_id.startswith("MAJORITY:")

    def test_new_order_stamps_the_engine_on_the_row(self) -> None:
        assert self._order(MAJORITY_ENGINE, 0).engine == MAJORITY_ENGINE
        assert self._order(DEFAULT_ENGINE, 0).engine == DEFAULT_ENGINE

    def test_new_order_defaults_to_twap(self) -> None:
        order = new_order(
            market_slug=self.SLUG,
            offset_seconds=3,
            index=0,
            generation=0,
            direction=Direction.UP,
            price=LIMIT_PRICE,
            size=Decimal("10"),
            now=1.0,
        )
        assert order.engine == DEFAULT_ENGINE
        assert order.order_id == "btc-updown-5m-1786263900:3:0:0"


# ── store round trip, K, L ───────────────────────────────────────────────────


def _order_row(slug: str, order_id: str, engine: str, state: OrderState) -> Order:
    return Order(
        order_id=order_id,
        market_slug=slug,
        offset_seconds=MAJORITY_WINDOW,
        direction=Direction.UP,
        price=LIMIT_PRICE,
        size=Decimal("10"),
        state=state,
        created_at=1.0,
        updated_at=1.0,
        venue_order_id=f"v-{order_id}",
        reprice_chain_id=order_id.rsplit(":", 1)[0],
        engine=engine,
    )


class TestEngineOwnershipRoundTrips:
    """Cases K and L. What goes into SQLite is what comes back out."""

    def test_order_engine_round_trips(self, store: Store) -> None:
        market = make_market(store)
        store.save_order(_order_row(market.slug, "a", MAJORITY_ENGINE, OrderState.SUBMITTED))
        store.save_order(_order_row(market.slug, "b", DEFAULT_ENGINE, OrderState.SUBMITTED))
        engines = {o.order_id: o.engine for o in store.orders_for(market.slug)}
        assert engines == {"a": MAJORITY_ENGINE, "b": DEFAULT_ENGINE}

    def test_an_order_saved_without_an_engine_reads_back_as_twap(self, store: Store) -> None:
        """Case K, at the model layer: the default is TWAP, never blank."""
        market = make_market(store)
        store.save_order(
            Order(
                order_id="legacy",
                market_slug=market.slug,
                offset_seconds=3,
                direction=Direction.UP,
                price=LIMIT_PRICE,
                size=Decimal("10"),
                state=OrderState.SUBMITTED,
            )
        )
        (loaded,) = store.orders_for(market.slug)
        assert loaded.engine == DEFAULT_ENGINE

    def test_fill_engine_round_trips(self, store: Store) -> None:
        market = make_market(store)
        store.save_order(_order_row(market.slug, "a", MAJORITY_ENGINE, OrderState.SUBMITTED))
        store.save_fill(
            Fill(
                fill_id="f1",
                order_id="a",
                market_slug=market.slug,
                size=Decimal("10"),
                price=LIMIT_PRICE,
                ts=2.0,
                engine=MAJORITY_ENGINE,
            )
        )
        (loaded,) = store.fills_for(market.slug)
        assert loaded.engine == MAJORITY_ENGINE

    def test_a_fill_saved_without_an_engine_reads_back_as_twap(self, store: Store) -> None:
        market = make_market(store)
        store.save_order(_order_row(market.slug, "a", DEFAULT_ENGINE, OrderState.SUBMITTED))
        store.save_fill(
            Fill(
                fill_id="f1",
                order_id="a",
                market_slug=market.slug,
                size=Decimal("10"),
                price=LIMIT_PRICE,
                ts=2.0,
            )
        )
        (loaded,) = store.fills_for(market.slug)
        assert loaded.engine == DEFAULT_ENGINE

    def test_ownership_survives_a_restart(self, tmp_path: Path) -> None:
        """Case L. A new Store on the same file, holding nothing in memory."""
        store = store_at(tmp_path)
        market = make_market(store)
        store.save_order(_order_row(market.slug, "m", MAJORITY_ENGINE, OrderState.SUBMITTED))
        store.save_order(_order_row(market.slug, "t", DEFAULT_ENGINE, OrderState.SUBMITTED))
        store.close()

        reopened = store_at(tmp_path)
        engines = {o.order_id: o.engine for o in reopened.orders_for(market.slug)}
        assert engines == {"m": MAJORITY_ENGINE, "t": DEFAULT_ENGINE}
        reopened.close()


# ── settlement: the risk path ────────────────────────────────────────────────


def _settlement(slug: str, engine: str, pnl: str, settled_at: float) -> Settlement:
    return Settlement(
        market_slug=slug,
        outcome=Outcome.UP,
        settlement_twap=Decimal("64100.00"),
        ptb=Decimal("64000.00"),
        settled_at=settled_at,
        pnl=Decimal(pnl),
        engine=engine,
    )


class TestBothEnginesReachTheSharedRiskAccounting:
    """The reason the settlements primary key had to be widened.

    settlement_history feeds _realised_losses, which feeds the daily-loss gate and
    the consecutive-loss gate. A settlement dropped by INSERT OR IGNORE would be a
    loss that never counted against a shared limit — the account would keep trading
    through a limit it had in fact breached.
    """

    def test_both_engines_settlements_coexist(self, store: Store) -> None:
        market = make_market(store)
        assert store.save_settlement(_settlement(market.slug, DEFAULT_ENGINE, "-5", 10.0))
        assert store.save_settlement(_settlement(market.slug, MAJORITY_ENGINE, "-7", 11.0))
        assert len(store.settlements_for(market.slug)) == 2

    def test_the_second_engine_is_not_silently_discarded(self, store: Store) -> None:
        """The exact bug: OR IGNORE reports a dropped row as an ordinary non-insert."""
        market = make_market(store)
        store.save_settlement(_settlement(market.slug, DEFAULT_ENGINE, "-5", 10.0))
        assert store.save_settlement(
            _settlement(market.slug, MAJORITY_ENGINE, "-7", 11.0)
        ) is True

    def test_a_duplicate_within_one_engine_is_still_refused(self, store: Store) -> None:
        market = make_market(store)
        store.save_settlement(_settlement(market.slug, MAJORITY_ENGINE, "-7", 11.0))
        assert store.save_settlement(
            _settlement(market.slug, MAJORITY_ENGINE, "999", 12.0)
        ) is False

    def test_neither_engine_overwrites_the_other(self, store: Store) -> None:
        market = make_market(store)
        store.save_settlement(_settlement(market.slug, DEFAULT_ENGINE, "-5", 10.0))
        store.save_settlement(_settlement(market.slug, MAJORITY_ENGINE, "-7", 11.0))
        twap = store.settlement_for(market.slug, engine=DEFAULT_ENGINE)
        majority = store.settlement_for(market.slug, engine=MAJORITY_ENGINE)
        assert twap is not None and twap.pnl == Decimal("-5")
        assert majority is not None and majority.pnl == Decimal("-7")

    def test_history_carries_both_losses(self, store: Store) -> None:
        """What the loss gates actually read."""
        market = make_market(store)
        store.save_settlement(_settlement(market.slug, DEFAULT_ENGINE, "-5", 10.0))
        store.save_settlement(_settlement(market.slug, MAJORITY_ENGINE, "-7", 11.0))
        total = sum(r.pnl for r in store.settlement_history(limit=100))
        assert total == Decimal("-12")

    def test_the_bare_lookup_defaults_to_twap_and_never_guesses(
        self, store: Store
    ) -> None:
        """No caller may depend on SQL row order to decide which engine it got."""
        market = make_market(store)
        store.save_settlement(_settlement(market.slug, MAJORITY_ENGINE, "-7", 11.0))
        store.save_settlement(_settlement(market.slug, DEFAULT_ENGINE, "-5", 10.0))
        found = store.settlement_for(market.slug)
        assert found is not None and found.engine == DEFAULT_ENGINE

    def test_settlements_for_is_deterministically_ordered(self, store: Store) -> None:
        market = make_market(store)
        store.save_settlement(_settlement(market.slug, MAJORITY_ENGINE, "-7", 11.0))
        store.save_settlement(_settlement(market.slug, DEFAULT_ENGINE, "-5", 10.0))
        assert [s.engine for s in store.settlements_for(market.slug)] == [
            MAJORITY_ENGINE,
            DEFAULT_ENGINE,
        ]


# ── intents ──────────────────────────────────────────────────────────────────


class TestIntentArbitrationIsPerEngine:
    """One intent per window PER ENGINE, which is what the rule always meant."""

    def test_twap_arbitration_is_unchanged(self, store: Store) -> None:
        make_market(store)
        assert store.save_intent(intent_for(offset_seconds=TWAP_OFFSET)) is True
        assert store.save_intent(intent_for(offset_seconds=TWAP_OFFSET)) is False

    def test_majority_may_hold_the_same_window(self, store: Store) -> None:
        market = make_market(store)
        store.save_intent(intent_for(offset_seconds=TWAP_OFFSET))
        majority = intent_for(
            offset_seconds=TWAP_OFFSET,
            intent_id=f"MAJORITY:{market.slug}:{TWAP_OFFSET}",
        )
        assert store.save_intent(majority, engine=MAJORITY_ENGINE) is True

    def test_a_majority_duplicate_is_still_refused(self, store: Store) -> None:
        market = make_market(store)
        first = intent_for(
            offset_seconds=TWAP_OFFSET,
            intent_id=f"MAJORITY:{market.slug}:{TWAP_OFFSET}",
        )
        store.save_intent(first, engine=MAJORITY_ENGINE)
        second = intent_for(
            offset_seconds=TWAP_OFFSET,
            size=Decimal("99"),
            intent_id=f"MAJORITY:{market.slug}:{TWAP_OFFSET}:again",
        )
        assert store.save_intent(second, engine=MAJORITY_ENGINE) is False

    def test_has_intent_is_engine_scoped(self, store: Store) -> None:
        market = make_market(store)
        store.save_intent(intent_for(offset_seconds=TWAP_OFFSET))
        assert store.has_intent(market.slug, TWAP_OFFSET) is True
        assert (
            store.has_intent(market.slug, TWAP_OFFSET, engine=MAJORITY_ENGINE) is False
        )

    def test_intent_keys_report_the_engine(self, store: Store) -> None:
        market = make_market(store)
        store.save_intent(intent_for(offset_seconds=TWAP_OFFSET))
        majority = intent_for(
            offset_seconds=TWAP_OFFSET,
            intent_id=f"MAJORITY:{market.slug}:{TWAP_OFFSET}",
        )
        store.save_intent(majority, engine=MAJORITY_ENGINE)
        assert set(store.intent_keys(market.slug)) == {
            (DEFAULT_ENGINE, TWAP_OFFSET),
            (MAJORITY_ENGINE, TWAP_OFFSET),
        }


# ── C, D. the replay guard ───────────────────────────────────────────────────


class TestTheReplayGuardWorksForBothEngines:
    """Cases C and D. `_existing` must resolve a persisted row after a restart.

    This is the defect the audit found: `_existing` recovered the market slug by
    taking everything before the first colon of the order id. With an engine prefix
    that yielded the engine NAME, so the lookup searched a market that does not
    exist, found nothing, and re-submitted an order that was already resting.
    """

    def test_a_twap_order_is_found_on_replay(self, tmp_path: Path) -> None:
        async def run() -> None:
            store = store_at(tmp_path)
            market = make_market(store)
            executor = PaperExecutor()
            first = await submitter(store, executor).submit(
                intent_for(), count=1, phase=MarketPhase.ACTIVE, now=float(WINDOW_TS)
            )
            again = await submitter(store, executor).submit(
                intent_for(), count=1, phase=MarketPhase.ACTIVE, now=float(WINDOW_TS)
            )
            assert [o.order_id for o in first] == [o.order_id for o in again]
            assert len(store.orders_for(market.slug)) == 1
            store.close()

        asyncio.run(run())

    def test_a_majority_order_is_found_on_replay(self, tmp_path: Path) -> None:
        """Without the fix this writes a SECOND order: a doubled live position."""

        async def run() -> None:
            store = store_at(tmp_path)
            market = make_market(store)
            executor = PaperExecutor()
            intent = intent_for()
            engine_submitter = Submitter_for(store, executor)
            first = await engine_submitter.submit(
                intent, count=1, phase=MarketPhase.ACTIVE, now=float(WINDOW_TS)
            )
            again = await Submitter_for(store, executor).submit(
                intent, count=1, phase=MarketPhase.ACTIVE, now=float(WINDOW_TS)
            )
            assert [o.order_id for o in first] == [o.order_id for o in again]
            assert len(store.orders_for(market.slug)) == 1
            assert store.orders_for(market.slug)[0].engine == MAJORITY_ENGINE
            store.close()

        asyncio.run(run())

    def test_the_majority_order_id_is_prefixed_end_to_end(self, tmp_path: Path) -> None:
        async def run() -> None:
            store = store_at(tmp_path)
            market = make_market(store)
            await Submitter_for(store, PaperExecutor()).submit(
                intent_for(), count=1, phase=MarketPhase.ACTIVE, now=float(WINDOW_TS)
            )
            (order,) = store.orders_for(market.slug)
            assert order.order_id.startswith(f"{MAJORITY_ENGINE}:")
            store.close()

        asyncio.run(run())


def Submitter_for(store: Store, executor: PaperExecutor) -> object:
    """A MAJORITY-owned submitter, built through the real constructor."""
    from arc.execution.submit import Submitter

    return Submitter(
        store,
        executor,
        bucket=__import__(
            "arc.execution.ratelimit", fromlist=["TokenBucket"]
        ).TokenBucket(sustained=1000, burst=1000, now=float(WINDOW_TS)),
        minimum=Decimal("5"),
        engine=MAJORITY_ENGINE,
    )


# ── E, F, G. sweeping ────────────────────────────────────────────────────────


class TestSweepIsolation:
    """Cases E, F and G — and G is the one that must NOT be isolated."""

    def _two_live_orders(self, store: Store, slug: str) -> None:
        store.save_order(
            _order_row(slug, "twap-live", DEFAULT_ENGINE, OrderState.SUBMITTED)
        )
        store.save_order(
            _order_row(slug, "maj-live", MAJORITY_ENGINE, OrderState.SUBMITTED)
        )

    def test_a_majority_sweep_leaves_twap_alone(self, store: Store) -> None:
        market = make_market(store)
        self._two_live_orders(store, market.slug)
        result = asyncio.run(
            sweeper(store, PaperExecutor()).sweep(
                market.slug, 2.0, engine=MAJORITY_ENGINE
            )
        )
        assert result.cancelled == ("maj-live",)
        states = {o.order_id: o.state for o in store.orders_for(market.slug)}
        assert states["twap-live"] is OrderState.SUBMITTED

    def test_a_twap_sweep_leaves_majority_alone(self, store: Store) -> None:
        market = make_market(store)
        self._two_live_orders(store, market.slug)
        result = asyncio.run(
            sweeper(store, PaperExecutor()).sweep(
                market.slug, 2.0, engine=DEFAULT_ENGINE
            )
        )
        assert result.cancelled == ("twap-live",)
        states = {o.order_id: o.state for o in store.orders_for(market.slug)}
        assert states["maj-live"] is OrderState.SUBMITTED

    def test_the_close_sweep_cancels_every_engine(self, store: Store) -> None:
        """Case G. The ONE deliberately engine-blind operation in the system.

        An order resting past close can fill against the settled outcome, and that
        position was approved by no gate. Filtering this sweep by engine would leave
        the other engine's order live through settlement.
        """
        market = make_market(store)
        self._two_live_orders(store, market.slug)
        result = asyncio.run(sweeper(store, PaperExecutor()).sweep(market.slug, 2.0))
        assert set(result.cancelled) == {"twap-live", "maj-live"}
        assert store.live_orders(market.slug) == ()

    def test_the_close_sweep_default_is_none_not_twap(self, store: Store) -> None:
        """A default of TWAP here would silently strand every MAJORITY order."""
        market = make_market(store)
        store.save_order(
            _order_row(market.slug, "maj-only", MAJORITY_ENGINE, OrderState.SUBMITTED)
        )
        result = asyncio.run(sweeper(store, PaperExecutor()).sweep(market.slug, 2.0))
        assert result.cancelled == ("maj-only",)


# ── I. reconciliation ────────────────────────────────────────────────────────


class TestReconciliationIsolation:
    """Case I. The other engine's orders are not orphans, and never were.

    The orphan set is built from EVERY local row rather than from the filtered
    subset. Built from the subset it would report every one of the other engine's
    legitimate orders as an orphan resting at the venue with no local record — a
    permanent false alarm that also blocks trading through the orphan gate.
    """

    def test_a_majority_pass_does_not_orphan_twap_orders(self, store: Store) -> None:
        market = make_market(store)
        executor = PaperExecutor()

        async def run() -> None:
            twap = _order_row(market.slug, "t1", DEFAULT_ENGINE, OrderState.SUBMITTED)
            maj = _order_row(market.slug, "m1", MAJORITY_ENGINE, OrderState.SUBMITTED)
            store.save_order(twap)
            store.save_order(maj)
            twap.venue_order_id = await executor.place(twap)
            maj.venue_order_id = await executor.place(maj)
            store.save_order(twap)
            store.save_order(maj)
            result = await reconciler(store, executor).reconcile(
                market.slug, 3.0, engine=MAJORITY_ENGINE
            )
            assert result.orphans == ()

        asyncio.run(run())

    def test_a_twap_pass_does_not_orphan_majority_orders(self, store: Store) -> None:
        market = make_market(store)
        executor = PaperExecutor()

        async def run() -> None:
            maj = _order_row(market.slug, "m1", MAJORITY_ENGINE, OrderState.SUBMITTED)
            store.save_order(maj)
            maj.venue_order_id = await executor.place(maj)
            store.save_order(maj)
            result = await reconciler(store, executor).reconcile(
                market.slug, 3.0, engine=DEFAULT_ENGINE
            )
            assert result.orphans == ()

        asyncio.run(run())

    def test_an_unscoped_pass_reconciles_both_engines(self, store: Store) -> None:
        """Recovery's case: a restarted process holds no state for EITHER engine."""
        market = make_market(store)
        executor = PaperExecutor()

        async def run() -> None:
            for order_id, engine in (("t1", DEFAULT_ENGINE), ("m1", MAJORITY_ENGINE)):
                order = _order_row(market.slug, order_id, engine, OrderState.SUBMITTED)
                store.save_order(order)
                order.venue_order_id = await executor.place(order)
                store.save_order(order)
            result = await reconciler(store, executor).reconcile(market.slug, 3.0)
            assert set(result.still_live) == {"t1", "m1"}
            assert result.orphans == ()

        asyncio.run(run())


# ── J. duplicate detection ───────────────────────────────────────────────────


class TestDuplicateDetectionIsEngineAware:
    """Case J. One order each is two engines behaving; two of one engine is a defect."""

    def test_one_order_per_engine_is_not_a_duplicate(self, store: Store) -> None:
        from arc.runtime.validation import _duplicate_live_orders

        market = make_market(store)
        store.save_order(_order_row(market.slug, "t1", DEFAULT_ENGINE, OrderState.SUBMITTED))
        store.save_order(_order_row(market.slug, "m1", MAJORITY_ENGINE, OrderState.SUBMITTED))
        assert _duplicate_live_orders(store, (market.slug,)) == []

    def test_two_live_orders_of_one_engine_is_still_a_duplicate(
        self, store: Store
    ) -> None:
        from arc.runtime.validation import _duplicate_live_orders

        market = make_market(store)
        store.save_order(_order_row(market.slug, "t1", DEFAULT_ENGINE, OrderState.SUBMITTED))
        store.save_order(_order_row(market.slug, "t2", DEFAULT_ENGINE, OrderState.SUBMITTED))
        offenders = _duplicate_live_orders(store, (market.slug,))
        assert len(offenders) == 1
        assert DEFAULT_ENGINE in offenders[0]

    def test_two_live_majority_orders_is_a_duplicate(self, store: Store) -> None:
        from arc.runtime.validation import _duplicate_live_orders

        market = make_market(store)
        store.save_order(_order_row(market.slug, "m1", MAJORITY_ENGINE, OrderState.SUBMITTED))
        store.save_order(_order_row(market.slug, "m2", MAJORITY_ENGINE, OrderState.SUBMITTED))
        offenders = _duplicate_live_orders(store, (market.slug,))
        assert len(offenders) == 1
        assert MAJORITY_ENGINE in offenders[0]

    def test_a_twap_intent_does_not_authorise_a_majority_order(
        self, store: Store
    ) -> None:
        """The write-before-act rule, per engine. Matching on the offset alone would
        let one engine's intent silently vouch for the other engine's order."""
        from arc.runtime.validation import _orphan_orders

        market = make_market(store)
        store.save_intent(intent_for(offset_seconds=MAJORITY_WINDOW))
        store.save_order(
            _order_row(market.slug, "m1", MAJORITY_ENGINE, OrderState.SUBMITTED)
        )
        assert _orphan_orders(store, (market.slug,)) == ["m1"]

    def test_an_engines_own_intent_authorises_its_order(self, store: Store) -> None:
        from arc.runtime.validation import _orphan_orders

        market = make_market(store)
        majority = intent_for(
            offset_seconds=MAJORITY_WINDOW,
            intent_id=f"MAJORITY:{market.slug}:{MAJORITY_WINDOW}",
        )
        store.save_intent(majority, engine=MAJORITY_ENGINE)
        store.save_order(
            _order_row(market.slug, "m1", MAJORITY_ENGINE, OrderState.SUBMITTED)
        )
        assert _orphan_orders(store, (market.slug,)) == []
