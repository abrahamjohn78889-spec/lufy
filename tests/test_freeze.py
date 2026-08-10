"""Window freeze: five values or none, persisted before anything can act on them.

The failure this whole module exists to prevent is not a window with missing values —
that is loud and obvious. It is a window holding a REAL opening_twap beside a DEFAULTED
buffer: a locked trigger that was never configured, on a window that looks completely
healthy in every log line and every dashboard panel (A12).

So the tests check atomicity from both ends:

    an in-memory validation failure   leaves all five None and the window PENDING
    a PERSISTENCE failure             leaves all five None and the window PENDING

The second is the one that needs the rollback. A window frozen in memory and unfrozen on
disk reloads after a restart as a window that never froze, while the running process has
already acted on its trigger.
"""

from __future__ import annotations

import logging
from decimal import Decimal
from pathlib import Path

import pytest
from conftest import OFFSETS, WINDOW_TS

from arc.config import TradingConfig, build_trading_config
from arc.domain.enums import Direction, MarketPhase, WindowState
from arc.domain.models import ExecutionWindow, MarketInstance, Observation
from arc.errors import NoDirectionError, StorageError, WindowFreezeError
from arc.storage.store import Store
from arc.windows.freeze import freeze_due_window, freeze_window, restore_window

FROZEN_VALUE_FIELDS = ("opening_twap", "ptb", "buffer", "direction", "locked_trigger")


def _trading(values: dict[str, str] | None = None) -> TradingConfig:
    from conftest import VALID_TRADING_VALUES

    return build_trading_config(dict(values or VALID_TRADING_VALUES))


def _market(store: Store, *, ptb: str = "64000.00", prices: tuple[str, ...] = ("64100.00",)) -> MarketInstance:
    market = MarketInstance.create(WINDOW_TS, OFFSETS)
    store.create_market(market, float(WINDOW_TS))
    market.phase = MarketPhase.ACTIVE
    market.freeze_ptb(ptb)
    for price in prices:
        market.add_observation(Observation(ts=float(WINDOW_TS), price=Decimal(price)))
    return market


def _assert_unfrozen(window: ExecutionWindow) -> None:
    """All five values None, plus state and frozen_at. The full atomicity assertion."""
    for name in FROZEN_VALUE_FIELDS:
        assert getattr(window, name) is None, f"{name} survived a rejected freeze"
    assert window.frozen_at is None
    assert window.state is WindowState.PENDING


class TestSuccessfulFreeze:
    def test_all_five_values_are_set_together(self, store: Store) -> None:
        market = _market(store)
        window = market.window(10)
        assert freeze_window(
            market, window, trading=_trading(), store=store, now=float(WINDOW_TS + 290)
        )
        for name in FROZEN_VALUE_FIELDS:
            assert getattr(window, name) is not None, f"{name} was not frozen"
        assert window.state is WindowState.FROZEN

    def test_the_configured_buffer_for_this_offset_is_used(self, store: Store) -> None:
        """Not a shared buffer and not a default. Per-offset, from config."""
        market = _market(store)
        trading = _trading()
        for offset in OFFSETS:
            window = market.window(offset)
            freeze_window(
                market, window, trading=trading, store=store, now=float(WINDOW_TS + 285)
            )
            assert window.buffer == trading.buffer_for(offset)

    def test_direction_is_up_when_twap_is_above_ptb(self, store: Store) -> None:
        market = _market(store, ptb="64000.00", prices=("64100.00",))
        window = market.window(10)
        freeze_window(market, window, trading=_trading(), store=store, now=float(WINDOW_TS))
        assert window.direction is Direction.UP
        assert window.locked_trigger == Decimal("64100.00") + Decimal("2.00")

    def test_direction_is_down_when_twap_is_below_ptb(self, store: Store) -> None:
        market = _market(store, ptb="64000.00", prices=("63900.00",))
        window = market.window(10)
        freeze_window(market, window, trading=_trading(), store=store, now=float(WINDOW_TS))
        assert window.direction is Direction.DOWN
        assert window.locked_trigger == Decimal("63900.00") - Decimal("2.00")

    def test_equality_yields_no_direction_and_no_freeze(self, store: Store) -> None:
        """The direction contract: twap == ptb is neither side, so nothing is frozen."""
        market = _market(store, ptb="64000.00", prices=("64000.00",))
        window = market.window(10)
        with pytest.raises(NoDirectionError):
            freeze_window(market, window, trading=_trading(), store=store, now=float(WINDOW_TS))
        for name in FROZEN_VALUE_FIELDS:
            assert getattr(window, name) is None, f"{name} was frozen on equality"
        assert window.state is WindowState.PENDING

    def test_all_five_windows_share_one_ptb(self, store: Store) -> None:
        """A12: the PTB is captured once and every window freezes against that number."""
        market = _market(store, ptb="64000.00")
        trading = _trading()
        for offset in OFFSETS:
            market.add_observation(Observation(ts=float(WINDOW_TS), price=Decimal("64010.00")))
            freeze_window(
                market, market.window(offset), trading=trading, store=store, now=float(WINDOW_TS)
            )
        ptbs = {market.window(o).ptb for o in OFFSETS}
        assert ptbs == {Decimal("64000.00")}

    def test_a_second_freeze_returns_false_rather_than_refreezing(self, store: Store) -> None:
        """Criterion 13: activation is idempotent, so repeated passes are the normal case."""
        market = _market(store)
        window = market.window(10)
        trading = _trading()
        assert freeze_window(market, window, trading=trading, store=store, now=float(WINDOW_TS))
        trigger = window.locked_trigger
        for _ in range(5):
            assert not freeze_window(
                market, window, trading=trading, store=store, now=float(WINDOW_TS + 1)
            )
        assert window.locked_trigger == trigger


class TestPartialFreezeIsRejected:
    """Criterion 3: any failure leaves all five values None."""

    def test_no_ptb_leaves_the_window_untouched(self, store: Store) -> None:
        market = MarketInstance.create(WINDOW_TS, OFFSETS)
        store.create_market(market, float(WINDOW_TS))
        market.phase = MarketPhase.ACTIVE
        market.add_observation(Observation(ts=float(WINDOW_TS), price=Decimal("64000.00")))
        window = market.window(10)
        with pytest.raises(WindowFreezeError):
            freeze_window(market, window, trading=_trading(), store=store, now=float(WINDOW_TS))
        _assert_unfrozen(window)

    def test_no_observations_leaves_the_window_untouched(self, store: Store) -> None:
        market = _market(store, prices=())
        window = market.window(10)
        with pytest.raises(WindowFreezeError):
            freeze_window(market, window, trading=_trading(), store=store, now=float(WINDOW_TS))
        _assert_unfrozen(window)

    def test_a_missing_buffer_leaves_the_window_untouched(self, store: Store) -> None:
        """A window with no buffer can never fire, so a default must not be invented.

        The buffer is read BEFORE the window is touched, which is why this leaves nothing
        behind: a config error must surface before any state changes, not after.
        """
        market = _market(store)
        market.windows[99] = ExecutionWindow(offset_seconds=99)
        window = market.window(99)
        with pytest.raises(WindowFreezeError, match="no configured buffer"):
            freeze_window(market, window, trading=_trading(), store=store, now=float(WINDOW_TS))
        _assert_unfrozen(window)

    def test_a_persistence_failure_rolls_the_freeze_back(self, store: Store) -> None:
        """The rollback path. A frozen-in-memory, unfrozen-on-disk window is the bug.

        The store is not modified to produce this; a subclass that raises is used, so
        production code is untouched (the same reason the AST gates use temporary
        fixtures rather than edits).
        """

        class RaisingStore(Store):
            def save_window_frozen(
                self, slug: str, window: ExecutionWindow, now: float
            ) -> bool:
                raise StorageError("disk is gone")

        market = _market(store)
        window = market.window(10)
        raising = RaisingStore.__new__(RaisingStore)
        with pytest.raises(WindowFreezeError, match="freeze rolled back"):
            freeze_window(
                market, window, trading=_trading(), store=raising, now=float(WINDOW_TS)
            )
        _assert_unfrozen(window)

    def test_a_missing_row_rolls_the_freeze_back(self, store: Store) -> None:
        """No row means the market was never persisted. The freeze must not stand."""
        market = MarketInstance.create(WINDOW_TS, OFFSETS)
        # Deliberately NOT created in the store.
        market.phase = MarketPhase.ACTIVE
        market.freeze_ptb("64000.00")
        market.add_observation(Observation(ts=float(WINDOW_TS), price=Decimal("64100.00")))
        window = market.window(10)
        with pytest.raises(WindowFreezeError, match="no row to update"):
            freeze_window(market, window, trading=_trading(), store=store, now=float(WINDOW_TS))
        _assert_unfrozen(window)

    def test_the_store_refuses_to_write_a_partial_row(self, store: Store) -> None:
        """Belt and braces: even handed a half-built window, the row is refused."""
        market = _market(store)
        window = market.window(10)
        window.opening_twap = Decimal("64000.00")
        window.state = WindowState.FROZEN
        with pytest.raises(StorageError, match="partially frozen"):
            store.save_window_frozen(market.slug, window, float(WINDOW_TS))

    def test_a_rejected_freeze_can_be_retried_on_a_later_pass(self, store: Store) -> None:
        """The realistic cause — no observations yet — resolves by itself next tick."""
        market = _market(store, prices=())
        window = market.window(10)
        trading = _trading()
        assert not freeze_due_window(
            market, window, trading=trading, store=store, now=float(WINDOW_TS)
        )
        _assert_unfrozen(window)
        market.add_observation(Observation(ts=float(WINDOW_TS), price=Decimal("64100.00")))
        assert freeze_due_window(
            market, window, trading=trading, store=store, now=float(WINDOW_TS + 1)
        )
        assert window.state is WindowState.FROZEN


class TestFreezeIsContained:
    """Criterion 18: one window's freeze failure must not stop the others."""

    def test_freeze_due_window_logs_a_warning_and_returns_false(
        self, store: Store, caplog: pytest.LogCaptureFixture
    ) -> None:
        market = _market(store, prices=())
        with caplog.at_level(logging.WARNING, logger="arc"):
            assert not freeze_due_window(
                market,
                market.window(10),
                trading=_trading(),
                store=store,
                now=float(WINDOW_TS),
                logger=logging.getLogger("arc.test.freeze"),
            )
        assert any("Window Freeze Rejected" in r.getMessage() for r in caplog.records)

    def test_it_is_not_logged_as_an_error(
        self, store: Store, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A window awaiting its first observation is not an error condition."""
        market = _market(store, prices=())
        with caplog.at_level(logging.DEBUG, logger="arc"):
            freeze_due_window(
                market,
                market.window(10),
                trading=_trading(),
                store=store,
                now=float(WINDOW_TS),
                logger=logging.getLogger("arc.test.freeze"),
            )
        assert not [r for r in caplog.records if r.levelno >= logging.ERROR]


class TestPersistedBeforeEvaluation:
    """Criterion 4: the trigger is on disk before any comparison against it can pass."""

    def test_the_row_holds_all_five_values_when_freeze_returns(self, store: Store) -> None:
        market = _market(store, ptb="64000.00", prices=("63900.00",))
        window = market.window(10)
        freeze_window(market, window, trading=_trading(), store=store, now=float(WINDOW_TS))
        row = store.restore_frozen(market.slug, 10)
        assert row is not None
        assert row["opening_twap"] == Decimal("63900.00")
        assert row["ptb"] == Decimal("64000.00")
        assert row["buffer"] == Decimal("2.00")
        assert row["direction"] is Direction.DOWN
        assert row["locked_trigger"] == Decimal("63898.00")
        assert row["state"] is WindowState.FROZEN

    def test_a_rolled_back_freeze_leaves_no_row(self, store: Store) -> None:
        market = _market(store, prices=())
        with pytest.raises(WindowFreezeError):
            freeze_window(
                market, market.window(10), trading=_trading(), store=store, now=float(WINDOW_TS)
            )
        assert store.restore_frozen(market.slug, 10) is None


class TestFrozenValuesAreImmutable:
    """Criterion 15."""

    def test_the_instance_refuses_a_second_freeze(self, store: Store) -> None:
        market = _market(store)
        freeze_window(
            market, market.window(10), trading=_trading(), store=store, now=float(WINDOW_TS)
        )
        with pytest.raises(WindowFreezeError, match="already frozen"):
            market.freeze_window(10, buffer=Decimal("9.99"), frozen_at=float(WINDOW_TS + 1))

    def test_a_moving_twap_does_not_move_a_locked_trigger(self, store: Store) -> None:
        market = _market(store, prices=("64100.00",))
        window = market.window(10)
        freeze_window(market, window, trading=_trading(), store=store, now=float(WINDOW_TS))
        locked = window.locked_trigger
        opening = window.opening_twap
        for _ in range(50):
            market.add_observation(Observation(ts=float(WINDOW_TS), price=Decimal("65000.00")))
        assert market.signal_twap != opening
        assert window.locked_trigger == locked
        assert window.opening_twap == opening


class TestRestoreIsVerbatim:
    def test_restore_returns_false_for_a_window_that_never_froze(self, store: Store) -> None:
        market = _market(store)
        assert not restore_window(market, 10, store=store)

    def test_restored_values_are_byte_identical(self, store: Store, tmp_path: Path) -> None:
        market = _market(store, ptb="64000.12345678", prices=("63999.87654321",))
        freeze_window(
            market, market.window(7), trading=_trading(), store=store, now=float(WINDOW_TS)
        )
        original = market.window(7)

        fresh = MarketInstance.create(WINDOW_TS, OFFSETS)
        assert restore_window(fresh, 7, store=store)
        restored = fresh.window(7)
        for name in FROZEN_VALUE_FIELDS:
            assert getattr(restored, name) == getattr(original, name), name
        assert restored.state is original.state


class TestEveryQuantityIsDecimal:
    """Criterion 20: float arithmetic is forbidden."""

    def test_frozen_values_are_all_decimal(self, store: Store) -> None:
        market = _market(store)
        window = market.window(10)
        freeze_window(market, window, trading=_trading(), store=store, now=float(WINDOW_TS))
        for name in ("opening_twap", "ptb", "buffer", "locked_trigger"):
            assert isinstance(getattr(window, name), Decimal), name

    def test_the_trigger_is_exact_to_the_cent(self, store: Store) -> None:
        """A float would give 64001.999999999996 here. Decimal gives 64002.00 exactly."""
        market = _market(store, ptb="63900.00", prices=("64000.00",))
        window = market.window(10)
        freeze_window(market, window, trading=_trading(), store=store, now=float(WINDOW_TS))
        assert window.locked_trigger == Decimal("64002.00")
        assert str(window.locked_trigger) == "64002.00"


class TestEqualityIsTerminalNotRetried:
    """The direction contract: equality is a final verdict, not a rejected freeze.

    A rejected freeze is retried on the next pass, which is correct when the cause is
    "no observation yet". Equality must NOT be retried: direction is determined once,
    at the opening instant, and a retry would freeze against a later TWAP.
    """

    def test_freeze_due_window_marks_no_direction(self, store: Store) -> None:
        market = _market(store, ptb="64000.00", prices=("64000.00",))
        window = market.window(10)
        assert not freeze_due_window(
            market, window, trading=_trading(), store=store, now=float(WINDOW_TS)
        )
        assert window.state is WindowState.NO_DIRECTION

    def test_a_later_moved_twap_cannot_freeze_a_direction(self, store: Store) -> None:
        market = _market(store, ptb="64000.00", prices=("64000.00",))
        window = market.window(10)
        trading = _trading()
        freeze_due_window(market, window, trading=trading, store=store, now=float(WINDOW_TS))
        for _ in range(20):
            market.add_observation(Observation(ts=float(WINDOW_TS), price=Decimal("70000.00")))
        assert not freeze_due_window(
            market, window, trading=trading, store=store, now=float(WINDOW_TS + 1)
        )
        assert window.state is WindowState.NO_DIRECTION
        assert window.direction is None

    def test_it_is_not_logged_as_an_error(
        self, store: Store, caplog: pytest.LogCaptureFixture
    ) -> None:
        market = _market(store, ptb="64000.00", prices=("64000.00",))
        with caplog.at_level(logging.DEBUG, logger="arc"):
            freeze_due_window(
                market,
                market.window(10),
                trading=_trading(),
                store=store,
                now=float(WINDOW_TS),
                logger=logging.getLogger("arc.test.freeze"),
            )
        assert not [r for r in caplog.records if r.levelno >= logging.ERROR]

    def test_it_survives_a_restart(self, store: Store) -> None:
        """Without the persisted state a reload would come back PENDING and re-decide."""
        market = _market(store, ptb="64000.00", prices=("64000.00",))
        freeze_due_window(
            market, market.window(10), trading=_trading(), store=store, now=float(WINDOW_TS)
        )
        fresh = MarketInstance.create(WINDOW_TS, OFFSETS)
        assert restore_window(fresh, 10, store=store)
        assert fresh.window(10).state is WindowState.NO_DIRECTION

    def test_the_remaining_windows_are_still_monitored(self, store: Store) -> None:
        """One window finding no direction must not end the market."""
        market = _market(store, ptb="64000.00", prices=("64000.00",))
        trading = _trading()
        assert not freeze_due_window(
            market, market.window(10), trading=trading, store=store, now=float(WINDOW_TS)
        )
        market.add_observation(Observation(ts=float(WINDOW_TS), price=Decimal("64200.00")))
        assert freeze_due_window(
            market, market.window(5), trading=trading, store=store, now=float(WINDOW_TS + 1)
        )
        assert market.window(5).state is WindowState.FROZEN
        assert market.window(5).direction is Direction.UP
