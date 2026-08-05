"""Determinism, and the ≥100-market runtime validation.

Two halves.

The first drives two independent runs of the SAME simulated observation stream through
the real rotator, the real window engine and the real decision engine, and asserts that
every frozen value, every risk verdict and every serialized intent is byte-identical.
Byte-identical rather than `==`, because two dataclasses compare equal for values that
print differently — Decimal("0.80") == Decimal("0.8") — and a venue receives the printed
form, not the object.

The second runs 120 consecutive markets and asserts the properties that only break at
scale: no duplicate intents, no window decided twice, no state carried from market N
into N+1, and the same fingerprint on a second run.

Nothing here is randomised. Prices come from index arithmetic, so a failure reproduces
exactly — which is the only reason asserting determinism is worth anything.
"""

from __future__ import annotations

import gc
from decimal import Decimal
from pathlib import Path

import pytest
from conftest import VALID_TRADING_VALUES, WINDOW_TS
from decision_fixtures import quote, trading

from arc.clock import FrozenClock
from arc.config import TradingConfig
from arc.decision.engine import DecisionEngine, RuntimeHealth
from arc.decision.quota import QuotaLedger
from arc.domain.enums import Direction, MarketPhase, SettlementSpecStatus, WindowState
from arc.domain.models import MarketInstance, Observation
from arc.market.rotation import MarketRotator
from arc.risk.limits import limits_from_trading
from arc.storage.store import Store
from arc.strategy.config import config_from_trading
from arc.strategy.registry import default_registry
from arc.windows.engine import WindowEngine

MARKET_COUNT = 120
STEP_SECONDS = 1.0
BASE_PRICE = Decimal("64000.00")
QUOTE = Decimal("0.70")

# TWAP inertia (A7): moving a 300-second mean by one buffer needs a spot deviation of
# buffer x (300 / window_seconds) — 20x to 100x. A flat run then a sustained late move is
# the only shape that actually crosses triggers; an oscillating walk never moves the mean
# and would report zero fires and prove nothing.
LATE_DEVIATIONS = (Decimal("0"), Decimal("300"), Decimal("600"))
LATE_WINDOW_SECONDS = 15
PTB_OFFSET = Decimal("50")


def _is_down_market(index: int) -> bool:
    return index % 2 == 1


def _ptb_for(index: int) -> Decimal:
    """A DOWN market's PTB sits above the flat price; an UP market's sits below it.

    Both are STRICTLY offset. Direction determination compares with `>` and `<` only,
    so a PTB equal to the flat price yields NO_DIRECTION, not UP.
    """
    return BASE_PRICE + (PTB_OFFSET if _is_down_market(index) else -PTB_OFFSET)


def _price_for(step: int) -> Decimal:
    index, second = divmod(step, 300)
    if second < 300 - LATE_WINDOW_SECONDS:
        return BASE_PRICE
    deviation = LATE_DEVIATIONS[index % len(LATE_DEVIATIONS)]
    if _is_down_market(index):
        return BASE_PRICE - deviation
    return BASE_PRICE + deviation


def _health() -> RuntimeHealth:
    """A runtime in which every gate that can pass, passes.

    Stated explicitly rather than defaulted, because the shipped default is DISABLED
    (A8) and a harness that quietly enabled trading would hide that.
    """
    return RuntimeHealth(trading_enabled=True, spec_status=SettlementSpecStatus.VERIFIED)


class _Run:
    """One full simulated run, wired exactly as production wires itself.

    The rotator drives the window engine, the window engine's fired windows drive the
    decision engine. Only the order-book quote and the process health reading are
    supplied by the harness, and both are genuinely external inputs.
    """

    def __init__(self, db: Path, *, market_count: int, config: TradingConfig) -> None:
        self.store = Store(db)
        self.store.migrate(0.0)
        self.trading = config
        self.clock = FrozenClock(now=float(WINDOW_TS))
        self.windows = WindowEngine(self.store, self.trading)
        self.decisions = DecisionEngine(
            self.store,
            strategy_config=config_from_trading(self.trading),
            limits=limits_from_trading(self.trading),
            registry=default_registry(),
            quota=QuotaLedger(
                max_trades_per_market=self.trading.max_trades_per_market,
                min_tradable_size=self.trading.min_tradable_size,
            ),
            quote_source=quote(QUOTE),
            health_source=_health,
        )
        self.rotator = MarketRotator(
            self.store,
            self.clock,
            offsets=self.trading.windows_by_priority,
            windows=self.windows,
            decisions=self.decisions,
        )
        self.market_count = market_count
        self.slugs: list[str] = []
        # (slug, offset) -> the exact text that would go on the wire.
        self.serialized: dict[tuple[str, int], str] = {}
        self.frozen: dict[tuple[str, int], tuple[str, str, str, str]] = {}
        self.state_paths: dict[tuple[str, int], list[str]] = {}
        self.max_live = 0

    def close(self) -> None:
        self.store.close()

    def _record(self, market: MarketInstance) -> None:
        for window in market.windows_by_priority():
            key = (market.slug, window.offset_seconds)
            path = self.state_paths.setdefault(key, [])
            if not path or path[-1] != window.state.value:
                path.append(window.state.value)
            if window.state is WindowState.FROZEN or window.state is WindowState.FIRED:
                assert window.direction is not None
                assert window.locked_trigger is not None
                assert window.opening_twap is not None
                assert window.buffer is not None
                self.frozen[key] = (
                    window.direction.value,
                    str(window.opening_twap),
                    str(window.locked_trigger),
                    str(window.buffer),
                )
        for intent in market.intents:
            key = (market.slug, intent.offset_seconds)
            rendered = intent.serialize()
            previous = self.serialized.get(key)
            assert previous is None or previous == rendered, (
                f"intent {key} was mutated after creation: {previous!r} -> {rendered!r}"
            )
            self.serialized[key] = rendered

    def _sample(self) -> None:
        for live in self.rotator.live:
            self._record(live)
        self.max_live = max(self.max_live, len(self.rotator.live))
        self.rotator.assert_at_most_two_live()

    def run(self) -> None:
        step = 0
        total_steps = int(self.market_count * 300 / STEP_SECONDS) + 1
        while step < total_steps:
            now = self.clock.now()
            event = self.rotator.advance(now)
            market = self.rotator.current
            if event.opened and market is not None:
                self.slugs.append(market.slug)
                # In production the PTB for market N is the venue's published finalPrice
                # for N-1. Simulated as a fixed per-market reference: this test's subject
                # is determinism, not PTB sourcing.
                market.freeze_ptb(_ptb_for(step // 300))
                self.store.save_ptb(market.slug, market.ptb or BASE_PRICE, now)

            # Sampled twice per step: a window can legitimately freeze on advance() and
            # fire on the next evaluation once the new observation has moved the TWAP.
            # Sampling once would record a transition the engine never made.
            self._sample()

            if market is not None and market.phase is MarketPhase.ACTIVE:
                market.add_observation(Observation(ts=now, price=_price_for(step)))
                self.rotator.evaluate_windows(now)

            self._sample()
            self.clock.advance(STEP_SECONDS)
            step += 1

    def fingerprint(self) -> tuple[object, ...]:
        """Everything two runs of the same input must agree on, as text."""
        return (
            tuple(self.slugs),
            tuple(sorted(self.frozen.items())),
            tuple(sorted(self.serialized.items())),
            tuple(sorted((k, tuple(v)) for k, v in self.state_paths.items())),
            self.decisions.intents_created,
            self.decisions.intents_denied,
            self.decisions.intents_skipped,
            self.windows.windows_frozen,
            self.windows.windows_fired,
            self.windows.windows_expired,
        )

    def rows(self) -> tuple[tuple[str, int, str], ...]:
        return tuple(
            (slug, intent.offset_seconds, intent.serialize())
            for slug in self.slugs
            for intent in self.store.intents_for(slug)
        )


def _run(
    tmp_path: Path,
    name: str,
    *,
    market_count: int = MARKET_COUNT,
    config: TradingConfig | None = None,
) -> _Run:
    run = _Run(
        tmp_path / name,
        market_count=market_count,
        config=config if config is not None else trading(),
    )
    run.run()
    return run


@pytest.fixture(scope="module")
def paired(tmp_path_factory: pytest.TempPathFactory) -> tuple[_Run, _Run]:
    """Two independent runs of the same stream, over a short span.

    Module-scoped because building them is the expensive part and every assertion in
    TestTwoIdenticalMarkets reads them without mutating them. Separate databases and
    separate engine instances, so nothing is shared but the input.
    """
    root = tmp_path_factory.mktemp("paired")
    first = _run(root, "a.db", market_count=6)
    second = _run(root, "b.db", market_count=6)
    return first, second


class TestTwoIdenticalMarkets:
    def test_the_run_actually_produced_intents(self, paired: tuple[_Run, _Run]) -> None:
        """Guard against the whole file passing vacuously. Two runs that both decided
        nothing are trivially identical and prove nothing about determinism."""
        first, _ = paired
        assert first.serialized, "no intent was ever created; the harness proves nothing"
        assert first.decisions.intents_created > 0

    def test_both_directions_occurred(self, paired: tuple[_Run, _Run]) -> None:
        """A12: UP fires on >= and DOWN on <=. A run that only ever went one way would
        leave the asymmetric comparison untested at scale."""
        first, _ = paired
        directions = {value[0] for value in first.frozen.values()}
        assert directions == {Direction.UP.value, Direction.DOWN.value}

    def test_the_frozen_values_are_identical(self, paired: tuple[_Run, _Run]) -> None:
        """Compared as text: Decimal("0.80") equals Decimal("0.8") but does not print
        the same, and the printed form is what reaches the venue."""
        first, second = paired
        assert first.frozen == second.frozen

    def test_every_serialized_intent_is_byte_identical(
        self, paired: tuple[_Run, _Run]
    ) -> None:
        first, second = paired
        assert set(first.serialized) == set(second.serialized)
        for key, rendered in first.serialized.items():
            assert second.serialized[key] == rendered, key

    def test_the_state_paths_are_identical(self, paired: tuple[_Run, _Run]) -> None:
        """Not just the endpoints. Two runs that reached FIRED by different routes would
        mean activation depended on something other than the observation stream."""
        first, second = paired
        assert first.state_paths == second.state_paths

    def test_the_counters_are_identical(self, paired: tuple[_Run, _Run]) -> None:
        first, second = paired
        assert (
            first.decisions.intents_created,
            first.decisions.intents_denied,
            first.decisions.intents_skipped,
        ) == (
            second.decisions.intents_created,
            second.decisions.intents_denied,
            second.decisions.intents_skipped,
        )

    def test_the_persisted_rows_are_identical(self, paired: tuple[_Run, _Run]) -> None:
        """Determinism has to survive the round trip through SQLite, or a restart would
        resume from values that differ from the ones the decision was made on."""
        first, second = paired
        assert first.rows() == second.rows()

    def test_the_whole_fingerprint_matches(self, paired: tuple[_Run, _Run]) -> None:
        first, second = paired
        assert first.fingerprint() == second.fingerprint()

    def test_the_intent_ids_carry_no_clock_and_no_counter(
        self, paired: tuple[_Run, _Run]
    ) -> None:
        """`slug:offset`. A uuid4 or a counter would make every run differ and would
        also stop a post-crash retry from recomputing the id the row already holds."""
        first, _ = paired
        for (slug, offset), rendered in first.serialized.items():
            assert f"intent_id={slug}:{offset}" in rendered

    def test_created_at_is_excluded_from_the_serialized_form(
        self, paired: tuple[_Run, _Run]
    ) -> None:
        """A wall-clock reading. Including it would make the determinism assertion a
        statement about the clock rather than about the decision."""
        first, _ = paired
        for rendered in first.serialized.values():
            assert "created_at" not in rendered

    def test_a_third_run_still_matches(self, tmp_path: Path, paired: tuple[_Run, _Run]) -> None:
        """Repeated runs produce identical results — not merely two of them."""
        first, _ = paired
        third = _run(tmp_path, "c.db", market_count=6)
        assert third.fingerprint() == first.fingerprint()
        third.close()


@pytest.fixture(scope="module")
def long_run(tmp_path_factory: pytest.TempPathFactory) -> _Run:
    return _run(tmp_path_factory.mktemp("long"), "long.db")


class TestOneHundredPlusMarkets:
    def test_every_market_on_the_grid_opened_exactly_once(self, long_run: _Run) -> None:
        assert len(long_run.slugs) >= MARKET_COUNT
        assert len(set(long_run.slugs)) == len(long_run.slugs)

    def test_at_most_two_markets_were_ever_live(self, long_run: _Run) -> None:
        """D6. A third would mean a closed market is still receiving observations."""
        assert long_run.max_live <= 2

    def test_no_window_holds_more_than_one_intent(self, long_run: _Run) -> None:
        """A12: exactly one intent per window, ever. Arbitrated by the SQLite UNIQUE
        constraint, so it survives a crash between the decision and the submission."""
        for slug in long_run.slugs:
            offsets = [i.offset_seconds for i in long_run.store.intents_for(slug)]
            assert len(offsets) == len(set(offsets)), slug

    def test_no_market_exceeded_its_trade_quota(self, long_run: _Run) -> None:
        limit = long_run.trading.max_trades_per_market
        for slug in long_run.slugs:
            assert len(long_run.store.intents_for(slug)) <= limit, slug

    def test_no_intent_was_created_for_an_unfired_window(self, long_run: _Run) -> None:
        """The window engine owns state; the decision engine only reads it. An intent
        for a window that never crossed its trigger would mean the decision layer had
        advanced a window itself."""
        for (slug, offset) in long_run.serialized:
            assert long_run.state_paths[(slug, offset)][-1] == WindowState.FIRED.value

    def test_every_intent_matches_its_windows_frozen_values(self, long_run: _Run) -> None:
        """Nothing is recomputed. A direction or trigger derived again at decision time
        would drift from the one the window locked."""
        for key, rendered in long_run.serialized.items():
            direction, opening, trigger, buffer = long_run.frozen[key]
            assert f"direction={direction}" in rendered
            assert f"opening_twap={opening}" in rendered
            assert f"locked_trigger={trigger}" in rendered
            assert f"buffer={buffer}" in rendered

    def test_every_window_reached_a_terminal_state(self, long_run: _Run) -> None:
        """No orphan left PENDING. A window that never terminates is a window that
        could still fire after its market closed."""
        terminal = {WindowState.FIRED.value, WindowState.EXPIRED.value}
        # The final market is still open when the run stops, so it is excluded.
        for (slug, offset), path in long_run.state_paths.items():
            if slug == long_run.slugs[-1]:
                continue
            assert path[-1] in terminal, (slug, offset, path)

    def test_no_intent_crossed_a_market_boundary(self, long_run: _Run) -> None:
        """A11: a new market is a new object. An intent naming a slug other than its
        own would mean per-market state had leaked at module scope."""
        for (slug, _), rendered in long_run.serialized.items():
            assert f"market_slug={slug}" in rendered

    def test_the_long_run_is_deterministic(self, tmp_path: Path, long_run: _Run) -> None:
        """The full 120-market fingerprint, twice. This is the runtime validation the
        phase requires, and it is what would catch a dict-ordering or float dependency
        that a six-market run is too short to expose."""
        again = _run(tmp_path, "again.db")
        assert again.fingerprint() == long_run.fingerprint()
        again.close()

    def test_wiring_the_decision_engine_added_no_retention(self, tmp_path: Path) -> None:
        """A11: persist, archive, DROP the reference.

        Asserted as a DELTA between a short run and a long one rather than as an
        absolute count, because the module-scoped fixtures in this file legitimately
        hold their own live markets and would otherwise be counted here. A rotator or
        engine that retained closed markets would show the difference in market count;
        a constant offset from other fixtures cancels.

        test_window_stress owns the same invariant for the rotator alone. This one
        exists because intents are now attached to the market instance, and an intent
        list held anywhere outside it would pin every market of a 24/7 run.
        """

        def alive_after(count: int, name: str) -> int:
            run = _run(tmp_path, name, market_count=count)
            fingerprint = run.fingerprint()
            assert fingerprint
            run.close()
            del run, fingerprint
            gc.collect()
            return len([o for o in gc.get_objects() if isinstance(o, MarketInstance)])

        short = alive_after(4, "mem-short.db")
        long = alive_after(40, "mem-long.db")
        assert long <= short, f"{short} -> {long} MarketInstances as markets grew 4 -> 40"


class TestTheExecutionBoundaryIsClean:
    """Spec A's Definition of Done: `arc/execution/` must not mention the strategy.

    The directory does not exist yet. The gate is wired now anyway, so the first file
    added there is checked on the pass that adds it rather than whenever someone
    remembers this rule.
    """

    FORBIDDEN = ("strategy", "twap", "ptb", "buffer")

    def test_the_execution_package_never_mentions_the_strategy(
        self, source_root: Path
    ) -> None:
        """A17: execution places and cancels orders. If it knew what a buffer was, the
        strategy would no longer be a pure function and a strategy change would have to
        be made in two places."""
        execution = source_root / "arc" / "execution"
        offenders: list[str] = []
        for path in sorted(execution.rglob("*.py")):
            text = path.read_text(encoding="utf-8").lower()
            offenders.extend(
                f"{path.name}: {word}" for word in self.FORBIDDEN if word in text
            )
        assert not offenders, offenders

    def test_the_decision_layer_is_where_those_words_live(self, source_root: Path) -> None:
        """The counterpart assertion. Without it the gate above would also pass if the
        words had simply been renamed everywhere, which would not be the same thing."""
        text = "".join(
            path.read_text(encoding="utf-8").lower()
            for path in sorted((source_root / "arc" / "decision").glob("*.py"))
        )
        assert all(word in text for word in self.FORBIDDEN)


class TestTheHarnessUsesRealComponents:
    def test_the_configuration_is_the_shipped_one(self) -> None:
        """If the run needed a special configuration to behave deterministically, the
        determinism would be a property of the harness rather than of the engine."""
        assert trading().max_trades_per_market == int(
            VALID_TRADING_VALUES["max_trades_per_market"]
        )

    def test_the_rotator_drives_the_decision_engine(self, paired: tuple[_Run, _Run]) -> None:
        """Not called directly by the harness. A test that called decide() itself would
        pass while the rotator never wired it up at all."""
        first, _ = paired
        assert first.decisions.intents_created == len(first.serialized)
