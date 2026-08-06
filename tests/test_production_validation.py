"""The Prompt 7 addendum items that nothing else already pins.

Four of the ten are covered elsewhere and are not repeated here:

  memory stability      test_continuous_runtime.py runs 500 markets under
                        tracemalloc with a mid-run restart.
  dashboard sync        test_dashboard_contract.py, test_ui_blindness.py,
                        test_ops_deck.py, test_countdown.py.
  signal tank           test_signal_tank.py, test_ws.py.
  ledger completeness   test_ledger.py.

What was NOT pinned anywhere, and is pinned here:

  item 1  determinism THROUGH THE V1 RUNTIME. test_determinism.py stops at the
          execution boundary; it says nothing about whether two identical paper
          runs produce the same ledger, the same recorder audit and the same
          validation report. Those are the three artefacts an operator actually
          reads before enabling live trading.
  item 3  no orphan task after a real `run()`. test_runtime_lifecycle.py replaces
          `ArcRuntime.run` with a stub, so the two tasks the real one creates —
          the feed loop and the Telegram notifier — are never created there and
          therefore never proven to be cancelled.
  item 4  replay is read-only. The validator, the recorder audit and the ledger
          are re-read over a recorded run and must return the same answers
          without changing one byte of any table.
  item 8  provider parity as BEHAVIOUR, not as a grep. test_providers.py proves
          no engine mentions a provider by name. This drives the same prices in
          through the RTDS payload shape and through ChainlinkFeed._translate
          and asserts the two runs are indistinguishable in every persisted row.

The harness is test_v1_run's: the real Store, Rotator, WindowEngine,
DecisionEngine, RiskEngine, Submitter and PaperExecutor, against a scripted
official book and scripted metadata. No network.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from collections.abc import AsyncIterator, Callable
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from test_chainlink import _FEED, _KEY, _SECRET, _full_report
from test_v1_run import CLOSE_TS, START_TS, FakeBook, _runtime

from arc.clock import FrozenClock
from arc.domain.timing import MARKET_DURATION_SECONDS
from arc.market.chainlink import ChainlinkFeed
from arc.runtime.engine import EXPECTED_SYMBOL, ArcRuntime, RuntimeStatus
from arc.runtime.ledger import ledger_records
from arc.runtime.recorder import audit_recording
from arc.runtime.validation import validate_run
from arc.storage.store import Store

OFFSETS = (15, 10, 7, 5, 3)
FLAT = Decimal("64100")
PUSH = Decimal("65000")


# ── the two providers, reduced to the frame each puts on the wire ────────────


def _rtds_frame(price: Decimal, ts: float) -> str:
    """The live RTDS envelope, confirmed against the relay on 2026-08-05."""
    return json.dumps(
        {
            "symbol": EXPECTED_SYMBOL,
            "timestamp": int(ts),
            "value": float(price),
            "full_accuracy_value": str(int(price * 10**18)),
        }
    )


def _chainlink_feed() -> ChainlinkFeed:
    return ChainlinkFeed(
        FrozenClock(float(START_TS)),
        api_key=_KEY,
        api_secret=_SECRET,
        feed_id=_FEED,
        decimals=18,
        symbol=EXPECTED_SYMBOL,
    )


def _chainlink_frame(price: Decimal, ts: float) -> str:
    """A real Data Streams report, decoded and translated by the real translator.

    Not a hand-written RTDS-shaped dict: the claim under test is that what
    ChainlinkFeed actually emits is what the runtime actually accepts, and a
    stand-in for the translator would prove that about the stand-in.
    """
    blob = _full_report(int(price * 10**18), observations_ts=int(ts))
    frame = json.dumps({"report": {"feedID": _FEED, "fullReport": "0x" + blob.hex()}})
    out = _chainlink_feed()._translate(frame)
    assert out is not None, "the translator dropped a well-formed report"
    return out


Emit = Callable[[Decimal, float], str]


# ── one V1 run, driven to a submission ───────────────────────────────────────


def _drive(run: ArcRuntime, clock: FrozenClock, emit: Emit) -> None:
    """One market from open to a fired window, one second at a time.

    Whole seconds throughout, so the two providers' timestamps are comparable:
    Chainlink's observationsTimestamp is an integer by the report's own schema,
    and a half-second step would truncate on one path and not the other — a
    difference in the harness, read as a difference between providers.
    """
    from test_v1_run import _pass  # local: keeps the import list honest about why

    def observe() -> None:
        run._handle_frame(emit(FLAT if clock.now() < CLOSE_TS - 15 else PUSH, clock.now()))

    observe()
    _pass(run, clock)
    while clock.now() < CLOSE_TS - 11:
        clock.advance(1.0)
        observe()
        _pass(run, clock)


def _fingerprint(store: Store) -> dict[str, Any]:
    """Every persisted row, plus the three artefacts an operator reads.

    Whole tables rather than a chosen few: a determinism test that only compares
    what it thought to compare passes on the field it forgot.
    """
    rows: dict[str, list[tuple[Any, ...]]] = {}
    conn = store.connection
    for table in store.table_names():
        rows[table] = [tuple(r) for r in conn.execute(f"SELECT * FROM {table}")]
    report = validate_run(
        store, offsets=OFFSETS, cadence_seconds=MARKET_DURATION_SECONDS
    ).as_json()
    # Wall-clock and file size describe the audit, not the run. Item 9 reports
    # them; comparing them here would make every determinism test flaky by design.
    report.pop("metrics", None)
    return {
        "rows": rows,
        "ledger": [r.as_json() for r in ledger_records(store)],
        "recorder": audit_recording(
            store, expected_windows=len(OFFSETS), cadence_seconds=MARKET_DURATION_SECONDS
        ).as_json(),
        "validation": report,
    }


def _run_once(tmp_path: Path, name: str, emit: Emit) -> dict[str, Any]:
    store = Store(tmp_path / name)
    store.migrate(float(START_TS))
    clock = FrozenClock(now=float(START_TS))
    run = _runtime(store, clock, book=FakeBook())
    _drive(run, clock, emit)
    print_ = _fingerprint(store)
    store.close()
    return print_


@pytest.fixture(scope="module")
def rtds_pair(tmp_path_factory: pytest.TempPathFactory) -> tuple[dict[str, Any], dict[str, Any]]:
    root = tmp_path_factory.mktemp("determinism")
    return _run_once(root, "a.db", _rtds_frame), _run_once(root, "b.db", _rtds_frame)


# ── item 1: two identical V1 paper runs ──────────────────────────────────────


class TestTwoIdenticalV1Runs:
    def test_the_run_actually_submitted_something(
        self, rtds_pair: tuple[dict[str, Any], dict[str, Any]]
    ) -> None:
        """Guard against passing vacuously: two runs that did nothing agree."""
        first, _ = rtds_pair
        assert first["rows"]["intents"], "no intent was created; this file proves nothing"
        assert first["rows"]["orders"], "no order was submitted"

    def test_every_persisted_row_is_identical(
        self, rtds_pair: tuple[dict[str, Any], dict[str, Any]]
    ) -> None:
        first, second = rtds_pair
        assert first["rows"] == second["rows"]

    def test_the_ledger_is_identical(
        self, rtds_pair: tuple[dict[str, Any], dict[str, Any]]
    ) -> None:
        first, second = rtds_pair
        assert first["ledger"] == second["ledger"]

    def test_the_recorder_audit_is_identical(
        self, rtds_pair: tuple[dict[str, Any], dict[str, Any]]
    ) -> None:
        first, second = rtds_pair
        assert first["recorder"] == second["recorder"]

    def test_the_validation_report_is_identical(
        self, rtds_pair: tuple[dict[str, Any], dict[str, Any]]
    ) -> None:
        """The artefact the go/no-go decision is made from. Two runs of the same
        data that disagreed here would make the verdict a property of the run
        rather than of the system."""
        first, second = rtds_pair
        assert first["validation"] == second["validation"]


# ── item 8: RTDS and Chainlink, same behaviour ───────────────────────────────


class TestTheProviderChangesNothingButTheSource:
    # The observations table records which feed a sample came from. That column is
    # the one thing item 8 EXPECTS to differ — "only the TWAP source changes" — so
    # it is blanked for the row comparisons and asserted on directly below.
    _FEED_ID = 4

    @pytest.fixture(scope="class")
    def crossed(
        self, tmp_path_factory: pytest.TempPathFactory
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        root = tmp_path_factory.mktemp("providers")
        return _run_once(root, "rtds.db", _rtds_frame), _run_once(
            root, "chainlink.db", _chainlink_frame
        )

    def _sourceless(self, rows: dict[str, list[tuple[Any, ...]]]) -> dict[str, Any]:
        blanked = [
            (*r[: self._FEED_ID], "", *r[self._FEED_ID + 1 :])
            for r in rows["observations"]
        ]
        return {**rows, "observations": blanked}

    def test_the_chainlink_run_really_ran(
        self, crossed: tuple[dict[str, Any], dict[str, Any]]
    ) -> None:
        """A translator that dropped every frame would leave both runs empty of
        observations, and every comparison below would pass on nothing."""
        _, chainlink = crossed
        assert chainlink["rows"]["observations"]
        assert chainlink["rows"]["orders"]

    def test_the_source_is_recorded_and_is_the_thing_that_differs(
        self, crossed: tuple[dict[str, Any], dict[str, Any]]
    ) -> None:
        """The provenance column. A run whose rows did not say where the price came
        from would make the two providers indistinguishable in the audit as well as
        in the behaviour — and only the second of those is the goal."""
        rtds, chainlink = crossed
        assert {r[self._FEED_ID] for r in chainlink["rows"]["observations"]} == {_FEED}
        assert {r[self._FEED_ID] for r in rtds["rows"]["observations"]} == {""}

    def test_the_same_observations_landed(
        self, crossed: tuple[dict[str, Any], dict[str, Any]]
    ) -> None:
        """Same timestamps, same exact prices. The signal TWAP is a cumulative mean
        of these, so equality here is what makes every later comparison meaningful."""
        rtds, chainlink = crossed
        assert self._sourceless(rtds["rows"])["observations"] == (
            self._sourceless(chainlink["rows"])["observations"]
        )

    def test_the_windows_froze_identically(
        self, crossed: tuple[dict[str, Any], dict[str, Any]]
    ) -> None:
        """Window Engine: the opening TWAP, the buffer, the direction and the
        locked trigger. If the provider reached these, it reached the strategy."""
        rtds, chainlink = crossed
        assert rtds["rows"]["windows"] == chainlink["rows"]["windows"]

    def test_the_same_intents_and_orders_were_produced(
        self, crossed: tuple[dict[str, Any], dict[str, Any]]
    ) -> None:
        rtds, chainlink = crossed
        assert rtds["rows"]["intents"] == chainlink["rows"]["intents"]
        assert rtds["rows"]["orders"] == chainlink["rows"]["orders"]

    def test_every_table_agrees(
        self, crossed: tuple[dict[str, Any], dict[str, Any]]
    ) -> None:
        """Every table, not a chosen few: a parity test that compares only what it
        thought to compare passes on the column it forgot."""
        rtds, chainlink = crossed
        assert self._sourceless(rtds["rows"]) == self._sourceless(chainlink["rows"])

    def test_the_validation_report_agrees(
        self, crossed: tuple[dict[str, Any], dict[str, Any]]
    ) -> None:
        rtds, chainlink = crossed
        assert rtds["validation"] == chainlink["validation"]
        assert rtds["ledger"] == chainlink["ledger"]


# ── item 4: replay reads, and only reads ─────────────────────────────────────


def _dump(store: Store) -> dict[str, list[tuple[Any, ...]]]:
    conn = store.connection
    return {
        t: [tuple(r) for r in conn.execute(f"SELECT * FROM {t}")]
        for t in store.table_names()
    }


class TestReplayIsReadOnlyAndRepeatable:
    @pytest.fixture
    def recorded(self, tmp_path: Path) -> Store:
        store = Store(tmp_path / "recorded.db")
        store.migrate(float(START_TS))
        clock = FrozenClock(now=float(START_TS))
        _drive(_runtime(store, clock, book=FakeBook()), clock, _rtds_frame)
        return store

    def test_replaying_changes_no_row(self, recorded: Store) -> None:
        """Item 4's hard clause. A validator that filled in a missing value would
        make the second reading of the same run describe the first reading."""
        before = _dump(recorded)
        for _ in range(3):
            _fingerprint(recorded)
        assert _dump(recorded) == before

    def test_replaying_changes_no_observation(self, recorded: Store) -> None:
        """Named separately because the observations table is the recording. The
        rest can be re-derived from it; it cannot be re-derived from anything."""
        slug = next(iter(r["slug"] for r in recorded.recent_markets(limit=1)))
        before = recorded.observations_for(slug)
        _fingerprint(recorded)
        assert recorded.observations_for(slug) == before

    def test_the_second_reading_answers_the_same(self, recorded: Store) -> None:
        assert _fingerprint(recorded) == _fingerprint(recorded)

    def test_the_database_stays_intact(self, recorded: Store) -> None:
        _fingerprint(recorded)
        assert recorded.integrity_check() == "ok"


# ── item 3: nothing is still running after a real run ────────────────────────


class _SilentFeed:
    """A provider that connects and then says nothing.

    The real feed loop is what item 3 is about: it must be cancelled by the run
    ending, not the other way round. A feed that ended by itself would let the
    task finish for a reason the shutdown path never had to produce.
    """

    url = "wss://test.invalid/silent"
    connect_attempts = 0

    async def messages(self) -> AsyncIterator[str | bytes]:
        await asyncio.Event().wait()
        yield b""  # unreachable; present so this is an async generator


def _tasks() -> set[asyncio.Task[Any]]:
    return {t for t in asyncio.all_tasks() if not t.done()}


class TestARealRunLeavesNothingBehind:
    """`test_runtime_lifecycle.py` replaces `run()` with a stub, so the feed task
    and the notifier task it creates are never created there. This runs the real
    one, bounded to a single market."""

    def _bounded(self, tmp_path: Path) -> tuple[ArcRuntime, set[asyncio.Task[Any]]]:
        store = Store(tmp_path / "arc.db")
        store.migrate(float(START_TS))
        clock = FrozenClock(now=float(START_TS))
        run = _runtime(store, clock, book=FakeBook())
        run._feed = _SilentFeed()  # type: ignore[assignment]

        async def _go() -> set[asyncio.Task[Any]]:
            before = _tasks()
            await run.run(market_target=1)
            # One turn for the cancelled tasks to be collected. `run()` awaits
            # them, so this is the loop's bookkeeping, not a wait for shutdown.
            await asyncio.sleep(0)
            return _tasks() - before

        return run, asyncio.run(_go())

    def test_no_task_outlives_the_run(self, tmp_path: Path) -> None:
        _, leftover = self._bounded(tmp_path)
        assert leftover == set(), f"still running: {[t.get_name() for t in leftover]}"

    def test_the_status_settles_on_stopped(self, tmp_path: Path) -> None:
        run, _ = self._bounded(tmp_path)
        assert run.status is RuntimeStatus.STOPPED

    def test_the_notifier_left_no_subscriber_in_the_hub(self, tmp_path: Path) -> None:
        """A queue left subscribed is broadcast into forever, and the hub holds a
        reference to it — the leak is a growing queue, not a visible task."""
        run, _ = self._bounded(tmp_path)
        assert run.hub.subscriber_count == 0

    def test_the_run_really_processed_a_market(self, tmp_path: Path) -> None:
        run, _ = self._bounded(tmp_path)
        assert run.stats.markets_processed == 1


def test_the_store_closes_without_a_lingering_connection(tmp_path: Path) -> None:
    """Item 3's last resource. Windows keeps the file locked while the connection
    is open, so a store that survived teardown breaks the next run's migration."""
    store = Store(tmp_path / "arc.db")
    store.migrate(float(START_TS))
    store.close()
    with pytest.raises(sqlite3.ProgrammingError):
        store.connection.execute("SELECT 1")
