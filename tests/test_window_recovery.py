"""Window recovery: frozen values reload verbatim, and recomputation is impossible.

Criterion 9. The failure mode is not a crash — it is a bot that comes back up, looks
entirely healthy, and trades a trigger nobody configured. The TWAP has MOVED since the
freeze (that movement is the whole thing the window is watching for), so a trigger
recomputed from the post-restart TWAP is a different number from the one the window
locked. Every log line and every dashboard panel still reads correctly.

So the tests do not merely check that restore works. They compute what a recomputing
implementation WOULD have produced and assert the restored value differs from it — the
only assertion that fails if someone quietly adds a recompute path.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest
from conftest import OFFSETS, VALID_TRADING_VALUES, WINDOW_TS

from arc.clock import FrozenClock
from arc.config import TradingConfig, build_trading_config
from arc.domain.enums import Direction, MarketPhase, WindowState
from arc.domain.models import MarketInstance, Observation, TwapAccumulator
from arc.errors import WindowFreezeError
from arc.storage.store import Store
from arc.windows.engine import WindowEngine
from arc.windows.freeze import freeze_window, restore_window

FREEZE_TS = float(WINDOW_TS + 290)


def _trading() -> TradingConfig:
    return build_trading_config(dict(VALID_TRADING_VALUES))


def _market(store: Store, *, ptb: str, price: str) -> MarketInstance:
    market = MarketInstance.create(WINDOW_TS, OFFSETS)
    store.create_market(market, float(WINDOW_TS))
    market.phase = MarketPhase.ACTIVE
    store.save_phase(market.slug, MarketPhase.ACTIVE, float(WINDOW_TS))
    market.freeze_ptb(ptb)
    store.save_ptb(market.slug, Decimal(ptb), float(WINDOW_TS))
    market.add_observation(Observation(ts=float(WINDOW_TS), price=Decimal(price)))
    store.save_accumulator(
        market.slug, market.running_sum, market.observation_count, float(WINDOW_TS)
    )
    return market


def _reopen(db: Path) -> Store:
    """A genuinely new Store on the same file. Nothing is carried in memory."""
    store = Store(db)
    store.migrate(1.0)
    return store


class TestFrozenValuesReloadVerbatim:
    def test_all_five_values_come_back_identical(self, tmp_path: Path) -> None:
        db = tmp_path / "arc.db"
        store = _reopen(db)
        market = _market(store, ptb="64000.12345678", price="63900.87654321")
        freeze_window(market, market.window(10), trading=_trading(), store=store, now=FREEZE_TS)
        before = market.window(10)
        expected = {
            "opening_twap": before.opening_twap,
            "ptb": before.ptb,
            "buffer": before.buffer,
            "direction": before.direction,
            "locked_trigger": before.locked_trigger,
            "frozen_at": before.frozen_at,
            "state": before.state,
        }
        store.close()

        # RESTART: new process, new Store, new MarketInstance object.
        store = _reopen(db)
        fresh = MarketInstance.create(WINDOW_TS, OFFSETS)
        assert restore_window(fresh, 10, store=store)
        after = fresh.window(10)
        for name, value in expected.items():
            assert getattr(after, name) == value, name
        store.close()

    def test_the_restored_object_is_a_different_object(self, tmp_path: Path) -> None:
        """A11: a new market is a NEW object. Nothing is reset and reused."""
        db = tmp_path / "arc.db"
        store = _reopen(db)
        market = _market(store, ptb="64000.00", price="63900.00")
        freeze_window(market, market.window(10), trading=_trading(), store=store, now=FREEZE_TS)
        fresh = MarketInstance.create(WINDOW_TS, OFFSETS)
        restore_window(fresh, 10, store=store)
        assert fresh is not market
        assert fresh.window(10) is not market.window(10)
        store.close()

    def test_every_frozen_window_is_restored_in_one_call(self, tmp_path: Path) -> None:
        db = tmp_path / "arc.db"
        store = _reopen(db)
        trading = _trading()
        market = _market(store, ptb="64000.00", price="63900.00")
        for offset in (15, 10, 7):
            freeze_window(market, market.window(offset), trading=trading, store=store, now=FREEZE_TS)
        store.close()

        store = _reopen(db)
        engine = WindowEngine(store, trading)
        fresh = MarketInstance.create(WINDOW_TS, OFFSETS)
        assert engine.restore(fresh) == (7, 10, 15)
        # The two that never froze stay PENDING and are still openable.
        assert fresh.window(5).state is WindowState.PENDING
        assert fresh.window(3).state is WindowState.PENDING
        store.close()


class TestRecomputationIsImpossible:
    """The assertion that actually protects criterion 9."""

    def test_the_restored_trigger_differs_from_a_recomputed_one(self, tmp_path: Path) -> None:
        db = tmp_path / "arc.db"
        store = _reopen(db)
        trading = _trading()
        market = _market(store, ptb="64000.00", price="63900.00")
        freeze_window(market, market.window(10), trading=trading, store=store, now=FREEZE_TS)
        locked = market.window(10).locked_trigger
        assert locked == Decimal("63898.00")

        # The TWAP moves after the freeze — which is exactly what the window watches for.
        for _ in range(99):
            market.add_observation(Observation(ts=float(WINDOW_TS), price=Decimal("63800.00")))
        moved = market.signal_twap
        assert moved is not None and moved != Decimal("63900.00")
        store.save_accumulator(
            market.slug, market.running_sum, market.observation_count, FREEZE_TS
        )
        store.close()

        # RESTART.
        store = _reopen(db)
        fresh = MarketInstance.create(WINDOW_TS, OFFSETS)
        fresh.restore_ptb("64000.00")
        row = store.load_market_row(fresh.slug)
        assert row is not None
        fresh.accumulator = TwapAccumulator.restore(
            str(row["running_sum"]), int(row["observation_count"])
        )
        assert restore_window(fresh, 10, store=store)

        restored = fresh.window(10).locked_trigger
        would_have_recomputed = moved - trading.buffer_for(10)
        assert restored == locked
        assert restored != would_have_recomputed, (
            "the restored trigger equals what a recomputing implementation would "
            "produce; this test can no longer detect the defect"
        )
        store.close()

    def test_the_restored_direction_differs_from_a_recomputed_one(self, tmp_path: Path) -> None:
        """Direction can flip outright, which is worse than a shifted trigger.

        Froze DOWN; by restart time the TWAP is above the PTB, so a recomputing
        implementation would restore it as UP and trade the opposite side.
        """
        db = tmp_path / "arc.db"
        store = _reopen(db)
        market = _market(store, ptb="64000.00", price="63900.00")
        freeze_window(market, market.window(10), trading=_trading(), store=store, now=FREEZE_TS)
        assert market.window(10).direction is Direction.DOWN

        market.accumulator = TwapAccumulator.restore("64500.00", 1)
        store.save_accumulator(
            market.slug, market.running_sum, market.observation_count, FREEZE_TS
        )
        store.close()

        store = _reopen(db)
        fresh = MarketInstance.create(WINDOW_TS, OFFSETS)
        fresh.restore_ptb("64000.00")
        restore_window(fresh, 10, store=store)
        assert fresh.window(10).direction is Direction.DOWN, (
            "a recomputing implementation would say UP here"
        )
        store.close()

    def test_restore_frozen_cannot_derive_direction_or_trigger(self) -> None:
        """Structural: both are REQUIRED keyword arguments with no defaults.

        There is no signature by which a caller could omit them and have the method
        work it out, so "restore never recomputes" is enforced by the API rather than
        by a convention someone has to remember.
        """
        window = MarketInstance.create(WINDOW_TS, OFFSETS).window(10)
        with pytest.raises(TypeError):
            window.restore_frozen(  # type: ignore[call-arg]
                opening_twap=Decimal("1"),
                ptb=Decimal("1"),
                buffer=Decimal("1"),
                frozen_at=0.0,
            )


class TestRestoredStateIsPreserved:
    def test_a_fired_window_comes_back_fired_and_cannot_fire_again(
        self, tmp_path: Path
    ) -> None:
        """Criterion 12 across a restart: one intent per window, ever."""
        db = tmp_path / "arc.db"
        store = _reopen(db)
        trading = _trading()
        market = _market(store, ptb="64000.00", price="64100.00")
        freeze_window(market, market.window(10), trading=trading, store=store, now=FREEZE_TS)
        market.accumulator = TwapAccumulator.restore("64200.00", 1)

        engine = WindowEngine(store, trading)
        assert engine.pass_over(market, FREEZE_TS).fired == (10,)
        store.close()

        store = _reopen(db)
        fresh = MarketInstance.create(WINDOW_TS, OFFSETS)
        fresh.restore_ptb("64000.00")
        fresh.accumulator = TwapAccumulator.restore("64200.00", 1)
        fresh.phase = MarketPhase.ACTIVE
        engine = WindowEngine(store, trading)
        engine.restore(fresh)
        assert fresh.window(10).state is WindowState.FIRED
        assert fresh.window(10).fired_at == FREEZE_TS
        # A post-restart pass with the trigger still satisfied must not re-fire it.
        assert engine.pass_over(fresh, FREEZE_TS + 1).fired == ()
        store.close()

    def test_an_expired_window_comes_back_expired(self, tmp_path: Path) -> None:
        db = tmp_path / "arc.db"
        store = _reopen(db)
        trading = _trading()
        market = _market(store, ptb="64000.00", price="64100.00")
        freeze_window(market, market.window(10), trading=trading, store=store, now=FREEZE_TS)
        engine = WindowEngine(store, trading)
        assert engine.expire_all(market)
        store.close()

        store = _reopen(db)
        fresh = MarketInstance.create(WINDOW_TS, OFFSETS)
        WindowEngine(store, trading).restore(fresh)
        assert fresh.window(10).state is WindowState.EXPIRED
        store.close()

    def test_a_frozen_window_comes_back_frozen_and_can_still_fire(
        self, tmp_path: Path
    ) -> None:
        """Recovery must not lose a live trigger, only refuse to change it."""
        db = tmp_path / "arc.db"
        store = _reopen(db)
        trading = _trading()
        market = _market(store, ptb="64000.00", price="64100.00")
        freeze_window(market, market.window(10), trading=trading, store=store, now=FREEZE_TS)
        store.close()

        store = _reopen(db)
        fresh = MarketInstance.create(WINDOW_TS, OFFSETS)
        fresh.phase = MarketPhase.ACTIVE
        fresh.restore_ptb("64000.00")
        fresh.accumulator = TwapAccumulator.restore("64102.00", 1)
        engine = WindowEngine(store, trading)
        engine.restore(fresh)
        assert fresh.window(10).state is WindowState.FROZEN
        assert engine.pass_over(fresh, FREEZE_TS + 1).fired == (10,)
        store.close()

    def test_restore_refuses_to_reload_into_pending(self) -> None:
        """PENDING is not a restorable state: a window that never froze has no row."""
        window = MarketInstance.create(WINDOW_TS, OFFSETS).window(10)
        with pytest.raises(WindowFreezeError):
            window.restore_frozen(
                opening_twap=Decimal("64000.00"),
                ptb=Decimal("64000.00"),
                buffer=Decimal("2.00"),
                direction=Direction.UP,
                locked_trigger=Decimal("64002.00"),
                frozen_at=FREEZE_TS,
                state=WindowState.PENDING,
            )


class TestRestoreThroughRotation:
    """The path a real restart takes: MarketRotator._restore, not restore_window directly."""

    def test_reopening_the_same_window_restores_its_frozen_windows(
        self, tmp_path: Path
    ) -> None:
        from arc.market.rotation import MarketRotator

        db = tmp_path / "arc.db"
        store = _reopen(db)
        trading = _trading()
        clock = FrozenClock(now=float(WINDOW_TS + 100))
        engine = WindowEngine(store, trading)
        rotator = MarketRotator(
            store, clock, offsets=trading.windows_by_priority, windows=engine
        )
        rotator.advance(clock.now())
        market = rotator.current
        assert market is not None
        market.freeze_ptb("64000.00")
        store.save_ptb(market.slug, Decimal("64000.00"), clock.now())
        market.add_observation(Observation(ts=clock.now(), price=Decimal("63900.00")))
        store.save_accumulator(
            market.slug, market.running_sum, market.observation_count, clock.now()
        )
        freeze_window(market, market.window(15), trading=trading, store=store, now=FREEZE_TS)
        locked = market.window(15).locked_trigger
        store.close()

        # RESTART inside the same 300-second window.
        store = _reopen(db)
        clock2 = FrozenClock(now=float(WINDOW_TS + 200))
        engine2 = WindowEngine(store, trading)
        rotator2 = MarketRotator(
            store, clock2, offsets=trading.windows_by_priority, windows=engine2
        )
        rotator2.advance(clock2.now())
        recovered = rotator2.current
        assert recovered is not None
        assert recovered.slug == market.slug
        assert recovered.ptb == Decimal("64000.00")
        assert recovered.window(15).state is WindowState.FROZEN
        assert recovered.window(15).locked_trigger == locked
        store.close()

    def test_a_dead_market_stays_dead_and_opens_no_window(self, tmp_path: Path) -> None:
        from arc.market.rotation import MarketRotator

        db = tmp_path / "arc.db"
        store = _reopen(db)
        trading = _trading()
        market = MarketInstance.create(WINDOW_TS, OFFSETS)
        store.create_market(market, float(WINDOW_TS))
        store.save_phase(market.slug, MarketPhase.DEAD, float(WINDOW_TS))
        store.close()

        store = _reopen(db)
        clock = FrozenClock(now=float(WINDOW_TS + 290))
        engine = WindowEngine(store, trading)
        rotator = MarketRotator(
            store, clock, offsets=trading.windows_by_priority, windows=engine
        )
        rotator.advance(clock.now())
        recovered = rotator.current
        assert recovered is not None
        assert recovered.phase is MarketPhase.DEAD
        assert all(
            w.state is WindowState.PENDING for w in recovered.windows_by_priority()
        )
        assert engine.windows_frozen == 0
        store.close()
