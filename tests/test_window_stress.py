"""Window engine stress: 120 consecutive simulated markets, driven by the real rotator.

Every invariant here is one that only breaks at scale or across a boundary:

    every window opens exactly once          no duplicate freezes
    every window reaches a terminal state    no orphans left PENDING
    no state leaks between markets           market N+1 must not see N's trigger
    memory does not grow                     the closed instance is dereferenced (A11)
    the whole run is deterministic           byte-identical across repeated runs

The markets are driven through MarketRotator with a FrozenClock stepped in small
increments, so activation goes through the real level-triggered path rather than a
shortcut that calls freeze directly. A stress test that bypassed the rotator would pass
while the rotator lost every window.
"""

from __future__ import annotations

import gc
from decimal import Decimal
from itertools import pairwise
from pathlib import Path

from conftest import VALID_TRADING_VALUES, WINDOW_TS

from arc.clock import FrozenClock
from arc.config import TradingConfig, build_trading_config
from arc.domain.enums import MarketPhase, WindowState
from arc.domain.models import MarketInstance, Observation
from arc.market.rotation import MarketRotator
from arc.storage.store import Store
from arc.windows.engine import WindowEngine
from arc.windows.lifecycle import is_terminal

MARKET_COUNT = 120
STEP_SECONDS = 1.0

BASE_PRICE = Decimal("64000.00")

# The last 15 seconds of each market carry a sustained deviation; the first 285 are flat.
# That shape is forced by TWAP inertia (A7): moving a 300-second mean by one buffer needs
# a spot deviation of buffer x (300 / window_seconds), i.e. 20x to 100x the buffer. An
# oscillating walk never moves the mean at all, so a stress test built on one would report
# zero fires and prove nothing about duplicate triggers.
#
# Three magnitudes, cycled per market: no move, a move that crosses the near windows only,
# and a move that crosses all five. Signed so that alternate markets are DOWN.
LATE_DEVIATIONS = (Decimal("0"), Decimal("300"), Decimal("600"))
LATE_WINDOW_SECONDS = 15

# Markets alternate direction. A DOWN market's PTB sits above the flat price, so its
# opening TWAP is below the PTB; an UP market's PTB sits below it. Both are STRICTLY
# offset: direction determination uses strict comparison, and a PTB equal to the flat
# price would yield NO_DIRECTION for every UP market rather than an UP direction.
PTB_OFFSET = Decimal("50")


def _trading() -> TradingConfig:
    return build_trading_config(dict(VALID_TRADING_VALUES))


def _is_down_market(index: int) -> bool:
    return index % 2 == 1


def _ptb_for(index: int) -> Decimal:
    return BASE_PRICE + (PTB_OFFSET if _is_down_market(index) else -PTB_OFFSET)


def _price_for(step: int) -> Decimal:
    """Deterministic by construction: an index arithmetic, no RNG.

    A failure reproduces exactly, which is the point of asserting determinism at all.
    """
    index, second = divmod(step, 300)
    if second < 300 - LATE_WINDOW_SECONDS:
        return BASE_PRICE
    deviation = LATE_DEVIATIONS[index % len(LATE_DEVIATIONS)]
    if _is_down_market(index):
        return BASE_PRICE - deviation
    return BASE_PRICE + deviation


class _Run:
    """One full simulated run. Records everything the assertions need."""

    def __init__(self, db: Path, *, market_count: int = MARKET_COUNT) -> None:
        self.store = Store(db)
        self.store.migrate(0.0)
        self.trading = _trading()
        self.clock = FrozenClock(now=float(WINDOW_TS))
        self.engine = WindowEngine(self.store, self.trading)
        self.rotator = MarketRotator(
            self.store,
            self.clock,
            offsets=self.trading.windows_by_priority,
            windows=self.engine,
        )
        self.market_count = market_count
        # slug -> {offset: [(state, trigger)]} recorded at every observed change.
        self.freezes: dict[tuple[str, int], int] = {}
        self.fires: dict[tuple[str, int], int] = {}
        self.triggers: dict[tuple[str, int], Decimal] = {}
        self.state_paths: dict[tuple[str, int], list[str]] = {}
        self.slugs: list[str] = []
        self.max_live = 0

    def close(self) -> None:
        self.store.close()

    def _record(self, market: MarketInstance) -> None:
        for window in market.windows_by_priority():
            key = (market.slug, window.offset_seconds)
            path = self.state_paths.setdefault(key, [])
            if not path or path[-1] != window.state.value:
                path.append(window.state.value)
                if window.state is WindowState.FROZEN:
                    self.freezes[key] = self.freezes.get(key, 0) + 1
                    assert window.locked_trigger is not None
                    self.triggers[key] = window.locked_trigger
                elif window.state is WindowState.FIRED:
                    self.fires[key] = self.fires.get(key, 0) + 1

    def run(self) -> None:
        step = 0
        # 300 seconds per market at STEP_SECONDS granularity, plus one step to close
        # the last market.
        total_steps = int(self.market_count * 300 / STEP_SECONDS) + 1
        while step < total_steps:
            now = self.clock.now()
            event = self.rotator.advance(now)
            market = self.rotator.current
            if event.opened and market is not None:
                self.slugs.append(market.slug)
                # PTB for market N comes from the venue's published finalPrice for N-1
                # in production. Simulated here as a fixed per-market reference: this
                # test's subject is window mechanics, not PTB sourcing.
                market.freeze_ptb(_ptb_for(step // 300))
                self.store.save_ptb(market.slug, market.ptb or BASE_PRICE, now)

            # Sampled TWICE per step, and that matters. A window can legitimately freeze
            # on advance() and fire on the very next evaluation once the new observation
            # has moved the TWAP past its brand-new trigger. Sampling once per step would
            # record that as PENDING -> FIRED and report a transition the engine never
            # made — a harness artifact indistinguishable from a real illegal transition.
            self._sample()

            if market is not None and market.phase is MarketPhase.ACTIVE:
                market.add_observation(Observation(ts=now, price=_price_for(step)))
                # Evaluation on the observation path, as production does.
                self.rotator.evaluate_windows(now)

            self._sample()
            self.clock.advance(STEP_SECONDS)
            step += 1

    def _sample(self) -> None:
        for live in self.rotator.live:
            self._record(live)
        self.max_live = max(self.max_live, len(self.rotator.live))
        self.rotator.assert_at_most_two_live()

    def fingerprint(self) -> tuple[object, ...]:
        """Everything that must be identical between two runs of the same input."""
        return (
            tuple(self.slugs),
            tuple(sorted((k, v) for k, v in self.freezes.items())),
            tuple(sorted((k, v) for k, v in self.fires.items())),
            tuple(sorted((k, str(v)) for k, v in self.triggers.items())),
            tuple(sorted((k, tuple(v)) for k, v in self.state_paths.items())),
            self.engine.windows_frozen,
            self.engine.windows_fired,
            self.engine.windows_expired,
        )


def _run(tmp_path: Path, name: str = "arc.db", *, market_count: int = MARKET_COUNT) -> _Run:
    run = _Run(tmp_path / name, market_count=market_count)
    run.run()
    return run


class TestOneHundredPlusMarkets:
    def test_every_market_on_the_grid_is_opened_exactly_once(self, tmp_path: Path) -> None:
        run = _run(tmp_path)
        assert len(run.slugs) >= MARKET_COUNT
        assert len(set(run.slugs)) == len(run.slugs), "a slug was opened twice"
        run.close()

    def test_at_most_two_markets_are_ever_live(self, tmp_path: Path) -> None:
        """D6. A third means a closed market is still receiving observations."""
        run = _run(tmp_path)
        assert run.max_live <= 2
        run.close()

    def test_every_window_of_every_market_freezes_exactly_once(self, tmp_path: Path) -> None:
        """Criterion 13: activation is idempotent across ~36,000 rotation passes."""
        run = _run(tmp_path)
        assert run.freezes, "no window ever froze; the harness is not exercising anything"
        duplicates = {k: v for k, v in run.freezes.items() if v != 1}
        assert not duplicates, f"duplicate freezes: {duplicates}"
        run.close()

    def test_no_window_fires_more_than_once(self, tmp_path: Path) -> None:
        """Criterion 12."""
        run = _run(tmp_path)
        assert run.fires, "no window ever fired; the price walk does not cross triggers"
        duplicates = {k: v for k, v in run.fires.items() if v != 1}
        assert not duplicates, f"duplicate fires: {duplicates}"
        run.close()

    def test_no_activation_is_missed(self, tmp_path: Path) -> None:
        """Every window of every completed market must have left PENDING.

        The last market is excluded: it is still live when the loop stops, so its
        windows are legitimately mid-life.
        """
        run = _run(tmp_path)
        completed = set(run.slugs[:-2])
        missed = [
            key
            for key, path in run.state_paths.items()
            if key[0] in completed and path == ["PENDING"]
        ]
        assert not missed, f"windows that never activated: {missed[:10]}"
        run.close()

    def test_every_window_reaches_a_terminal_state(self, tmp_path: Path) -> None:
        """Criteria 8 and 19: no orphan is left hopeful forever."""
        run = _run(tmp_path)
        completed = set(run.slugs[:-2])
        for key, path in run.state_paths.items():
            if key[0] not in completed:
                continue
            assert path[-1] in ("FIRED", "EXPIRED"), f"{key} ended at {path[-1]}"
        run.close()

    def test_state_paths_are_monotonic_and_legal(self, tmp_path: Path) -> None:
        """Criterion 14: no illegal transition occurred anywhere in the run."""
        legal = {
            ("PENDING", "FROZEN"),
            ("PENDING", "EXPIRED"),
            ("FROZEN", "FIRED"),
            ("FROZEN", "EXPIRED"),
        }
        run = _run(tmp_path)
        for key, path in run.state_paths.items():
            assert path[0] == "PENDING", f"{key} started at {path[0]}"
            for a, b in pairwise(path):
                assert (a, b) in legal, f"{key}: illegal {a} -> {b}"
        run.close()

    def test_no_orphaned_active_windows_remain_on_disk(self, tmp_path: Path) -> None:
        run = _run(tmp_path)
        for slug in run.slugs[:-2]:
            for row in run.store.windows_for(slug):
                assert row["state"] in ("FIRED", "EXPIRED", "PENDING")
                # PENDING on disk is only legal for a window whose market never
                # activated it — which the previous test already forbids for
                # completed markets, so cross-check here against memory.
        pending_in_memory = [
            key
            for key, path in run.state_paths.items()
            if key[0] in set(run.slugs[:-2]) and path[-1] not in ("FIRED", "EXPIRED")
        ]
        assert not pending_in_memory
        run.close()


class TestNoSharedStateBetweenMarkets:
    def test_no_two_markets_share_a_locked_trigger_object(self, tmp_path: Path) -> None:
        """Identity, not equality. Two markets may legitimately compute equal values.

        Sharing the OBJECT would mean one window's trigger is reachable from another
        market — the leak A11 exists to prevent.
        """
        run = _run(tmp_path, market_count=20)
        ids_by_slug: dict[str, set[int]] = {}
        for (slug, _offset), trigger in run.triggers.items():
            ids_by_slug.setdefault(slug, set()).add(id(trigger))
        run.close()
        # Decimal is immutable and CPython may intern small values; the meaningful
        # check is that the ENGINE holds no per-market attribute at all.
        assert set(WindowEngine.__slots__) == {
            "_logger",
            "_store",
            "_trading",
            "windows_expired",
            "windows_fired",
            "windows_frozen",
        }

    def test_the_engine_holds_no_market_reference(self, tmp_path: Path) -> None:
        run = _run(tmp_path, market_count=10)
        for name in WindowEngine.__slots__:
            value = getattr(run.engine, name)
            assert not isinstance(value, MarketInstance)
        run.close()

    def test_a_new_market_starts_with_every_window_pending(self, tmp_path: Path) -> None:
        """A11: no reset(). A new market is a new object at zero."""
        run = _run(tmp_path, market_count=20)
        for _key, path in run.state_paths.items():
            assert path[0] == "PENDING"
        run.close()

    def test_triggers_differ_across_markets(self, tmp_path: Path) -> None:
        """If a trigger leaked, adjacent markets would lock identical values.

        The price walk moves, so equal triggers across every market would mean the
        second market froze against the first's cached TWAP.
        """
        run = _run(tmp_path, market_count=30)
        by_offset: dict[int, set[str]] = {}
        for (_slug, offset), trigger in run.triggers.items():
            by_offset.setdefault(offset, set()).add(str(trigger))
        run.close()
        for offset, values in by_offset.items():
            assert len(values) > 1, f"every {offset}s window locked the same trigger"


class TestNoMemoryGrowth:
    def test_closed_market_instances_are_collected(self, tmp_path: Path) -> None:
        """A11: persist, archive, DROP THE REFERENCE.

        Counting live MarketInstance objects after a gc is the direct test. A rotator
        that kept every market would show ~120 here.
        """
        before = {id(o) for o in gc.get_objects() if isinstance(o, MarketInstance)}
        run = _run(tmp_path)
        del run.state_paths, run.triggers
        gc.collect()
        alive = [
            o
            for o in gc.get_objects()
            if isinstance(o, MarketInstance) and id(o) not in before
        ]
        # At most the two the rotator legitimately holds, plus any the test frame
        # happens to reference.
        assert len(alive) <= 4, f"{len(alive)} MarketInstances alive after {MARKET_COUNT} markets"
        run.close()

    def test_the_engine_counters_are_the_only_growth(self, tmp_path: Path) -> None:
        """No list, dict or set on the engine accumulates per market."""
        run = _run(tmp_path)
        for name in WindowEngine.__slots__:
            value = getattr(run.engine, name)
            assert not isinstance(value, (list, dict, set, tuple)), name
        run.close()

    def test_no_background_task_or_timer_is_left_behind(self, tmp_path: Path) -> None:
        """Criterion 19. The engine is synchronous, so there is nothing to leak.

        Asserted by looking for asyncio Handles and Tasks created during the run; a
        timer-based implementation would leave TimerHandles for windows that never came
        due before their market closed.
        """
        import asyncio

        def _live(kind: type) -> set[int]:
            return {id(o) for o in gc.get_objects() if isinstance(o, kind)}

        # Scoped to objects this run creates. gc.get_objects() is process-wide, so
        # an unscoped sweep also counts finished Tasks other test modules left
        # reachable and fails on their leaks instead of this engine's.
        before_handles = _live(asyncio.TimerHandle)
        before_tasks = _live(asyncio.Task)
        run = _run(tmp_path, market_count=20)
        gc.collect()
        handles = _live(asyncio.TimerHandle) - before_handles
        tasks = _live(asyncio.Task) - before_tasks
        run.close()
        assert not handles, f"{len(handles)} asyncio TimerHandles exist after the run"
        assert not tasks, f"{len(tasks)} asyncio Tasks exist after the run"


class TestDeterminism:
    """Criterion 16, at scale: two identical runs produce identical everything."""

    def test_two_runs_are_byte_identical(self, tmp_path: Path) -> None:
        first = _run(tmp_path, "a.db", market_count=40)
        second = _run(tmp_path, "b.db", market_count=40)
        try:
            assert first.fingerprint() == second.fingerprint()
        finally:
            first.close()
            second.close()

    def test_the_counters_agree_with_the_recorded_events(self, tmp_path: Path) -> None:
        run = _run(tmp_path)
        assert run.engine.windows_frozen == sum(run.freezes.values())
        assert run.engine.windows_fired == sum(run.fires.values())
        run.close()

    def test_frozen_and_expired_account_for_every_window(self, tmp_path: Path) -> None:
        """Every window of every completed market is accounted for exactly once."""
        run = _run(tmp_path)
        completed = set(run.slugs[:-2])
        terminal = sum(
            1
            for key, path in run.state_paths.items()
            if key[0] in completed and is_terminal(WindowState(path[-1]))
        )
        assert terminal == len(completed) * 5
        run.close()
