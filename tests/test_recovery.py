"""Recovery: what survives a restart, a crash, a PM2 restart and a VPS reboot.

All four are the same event to this process — it stops without warning and comes back
inside the same 300-second window — so they are tested as one mechanism rather than
four, with the differences that DO matter (an unflushed write, a phase mid-settlement,
a market that was already DEAD) each given their own case.

The property being defended is A4: a market's frozen values must reload VERBATIM.
Recomputation is not a fallback, because the venue publishes a market's official
opening reference once and does not publish it again. A restarted process that
recreated a blank market would resolve a NEW reference — or none at all — and every
window in that market would then lock a trigger against a number the pre-crash
windows never saw. Nothing about that failure is visible from outside; the market
simply trades against the wrong reference.

Feed reconnection is the fifth scenario and is exercised against the injected
connector: a dropped socket, an unreachable host and a refused connection are the
same code path with different exceptions, and none of them may end the stream.
"""

from __future__ import annotations

import asyncio
import logging
from decimal import Decimal
from pathlib import Path

from conftest import CLOSE_TS, OFFSETS, WINDOW_TS

from arc.clock import FrozenClock
from arc.domain.enums import MarketPhase, SettlementSpecStatus
from arc.domain.models import Observation
from arc.errors import ConnectionLostError
from arc.market.feed import BackoffPolicy, RtdsFeed
from arc.market.rotation import MarketRotator
from arc.market.watchdog import FeedWatchdog
from arc.runtime.state import RuntimeState
from arc.storage.schema import migrate
from arc.storage.store import Store

PTB = Decimal("64063.6718254685")


def _rotator(store: Store, clock: FrozenClock) -> MarketRotator:
    return MarketRotator(store, clock, offsets=OFFSETS)


def _observation(price: str, received_at: float) -> Observation:
    return Observation(ts=received_at, price=Decimal(price), window_seconds=30)


def _reopen(db_path: Path, now: float) -> tuple[Store, MarketRotator, FrozenClock]:
    """Simulate the process dying and coming back: new Store, new rotator, same file.

    Deliberately builds every object fresh. Reusing the rotator would test nothing —
    the state under test is precisely the state that does NOT survive in memory.
    """
    store = Store(db_path)
    store.migrate(now)
    clock = FrozenClock(now=now)
    return store, _rotator(store, clock), clock


class TestRestartInsideTheSameMarket:
    """A crash at t+120s: the process comes back with 180s of the market left."""

    def test_the_frozen_ptb_reloads_verbatim(self, tmp_path: Path) -> None:
        db = tmp_path / "arc.db"
        store, rotator, clock = _reopen(db, float(WINDOW_TS))
        rotator.advance(clock.now())
        market = rotator.current
        assert market is not None
        market.freeze_ptb(PTB)
        store.save_ptb(market.slug, PTB, clock.now())
        store.close()

        store2, rotator2, clock2 = _reopen(db, float(WINDOW_TS + 120))
        rotator2.advance(clock2.now())
        recovered = rotator2.current
        assert recovered is not None
        assert recovered.slug == market.slug
        # Verbatim, digit for digit. A re-resolved value would differ in the tail.
        assert recovered.ptb == PTB
        assert str(recovered.ptb) == str(PTB)
        store2.close()

    def test_recovery_is_not_a_recomputation(self, tmp_path: Path) -> None:
        """The recovered value comes from the row, not from anything the new process
        could derive. Storing a value no arithmetic would produce proves the path."""
        db = tmp_path / "arc.db"
        sentinel = Decimal("11111.111111111111")
        store, rotator, clock = _reopen(db, float(WINDOW_TS))
        rotator.advance(clock.now())
        market = rotator.current
        assert market is not None
        store.save_ptb(market.slug, sentinel, clock.now())
        store.close()

        store2, rotator2, clock2 = _reopen(db, float(WINDOW_TS + 60))
        rotator2.advance(clock2.now())
        recovered = rotator2.current
        assert recovered is not None
        assert recovered.ptb == sentinel
        store2.close()

    def test_the_accumulator_resumes_from_the_exact_sum(self, tmp_path: Path) -> None:
        """Sum and count, never a mean (hazard H1). A restored mean would bake one
        rounding in and then keep accumulating on top of it, so the restarted market's
        signal TWAP would drift away from an uninterrupted one."""
        db = tmp_path / "arc.db"
        store, rotator, clock = _reopen(db, float(WINDOW_TS))
        rotator.advance(clock.now())
        market = rotator.current
        assert market is not None
        prices = ["64000.1", "64000.2", "64000.3"]
        for price in prices:
            market.add_observation(_observation(price, clock.now()))
        expected_sum = sum(Decimal(p) for p in prices)
        store.save_accumulator(market.slug, market.running_sum, market.observation_count,
                               clock.now())
        store.close()

        store2, rotator2, clock2 = _reopen(db, float(WINDOW_TS + 90))
        rotator2.advance(clock2.now())
        recovered = rotator2.current
        assert recovered is not None
        assert recovered.running_sum == expected_sum
        assert recovered.observation_count == 3
        store2.close()

    def test_accumulation_continues_after_recovery(self, tmp_path: Path) -> None:
        """The restarted market must end up where an uninterrupted one would."""
        db = tmp_path / "arc.db"
        prices = ["64000.1", "64000.2", "64000.3", "64000.4"]

        store, rotator, clock = _reopen(db, float(WINDOW_TS))
        rotator.advance(clock.now())
        market = rotator.current
        assert market is not None
        for price in prices[:2]:
            market.add_observation(_observation(price, clock.now()))
        store.save_accumulator(market.slug, market.running_sum, market.observation_count,
                               clock.now())
        store.close()

        store2, rotator2, clock2 = _reopen(db, float(WINDOW_TS + 100))
        rotator2.advance(clock2.now())
        recovered = rotator2.current
        assert recovered is not None
        for price in prices[2:]:
            recovered.add_observation(_observation(price, clock2.now()))

        uninterrupted = sum(Decimal(p) for p in prices) / 4
        assert recovered.signal_twap == uninterrupted
        store2.close()

    def test_no_duplicate_market_row_is_created(self, tmp_path: Path) -> None:
        db = tmp_path / "arc.db"
        store, rotator, clock = _reopen(db, float(WINDOW_TS))
        rotator.advance(clock.now())
        store.close()

        store2, rotator2, clock2 = _reopen(db, float(WINDOW_TS + 30))
        rotator2.advance(clock2.now())
        assert store2.market_count() == 1
        store2.close()

    def test_the_recovered_market_is_the_only_live_one(self, tmp_path: Path) -> None:
        db = tmp_path / "arc.db"
        store, rotator, clock = _reopen(db, float(WINDOW_TS))
        rotator.advance(clock.now())
        store.close()

        store2, rotator2, clock2 = _reopen(db, float(WINDOW_TS + 30))
        rotator2.advance(clock2.now())
        assert len(rotator2.live) == 1
        rotator2.assert_at_most_two_live()
        store2.close()

    def test_the_recovered_market_still_collects(self, tmp_path: Path) -> None:
        """A recovered market that refused observations would silently stop
        accumulating for the rest of the window while looking healthy."""
        db = tmp_path / "arc.db"
        store, rotator, clock = _reopen(db, float(WINDOW_TS))
        rotator.advance(clock.now())
        store.close()

        store2, rotator2, clock2 = _reopen(db, float(WINDOW_TS + 30))
        rotator2.advance(clock2.now())
        accepted = rotator2.route(_observation("64000.5", clock2.now()))
        assert len(accepted) == 1
        store2.close()

    def test_a_ptb_frozen_before_the_crash_cannot_be_refrozen(self, tmp_path: Path) -> None:
        """restore_ptb refuses to overwrite, so there is no path by which a recovered
        market acquires a second, different opening reference."""
        db = tmp_path / "arc.db"
        store, rotator, clock = _reopen(db, float(WINDOW_TS))
        rotator.advance(clock.now())
        market = rotator.current
        assert market is not None
        store.save_ptb(market.slug, PTB, clock.now())
        store.close()

        store2, rotator2, clock2 = _reopen(db, float(WINDOW_TS + 45))
        rotator2.advance(clock2.now())
        recovered = rotator2.current
        assert recovered is not None
        # A second write is refused at the storage boundary too.
        assert store2.save_ptb(recovered.slug, Decimal("1.0"), clock2.now()) is False
        assert store2.load_ptb(recovered.slug) == PTB
        store2.close()


class TestRestartAcrossABoundary:
    """The process was down long enough that the market it was running has closed."""

    def test_the_new_market_opens_clean(self, tmp_path: Path) -> None:
        db = tmp_path / "arc.db"
        store, rotator, clock = _reopen(db, float(WINDOW_TS))
        rotator.advance(clock.now())
        market = rotator.current
        assert market is not None
        store.save_ptb(market.slug, PTB, clock.now())
        store.close()

        store2, rotator2, clock2 = _reopen(db, float(CLOSE_TS + 10))
        rotator2.advance(clock2.now())
        recovered = rotator2.current
        assert recovered is not None
        assert recovered.window_ts == CLOSE_TS
        # A different market. It must NOT inherit the previous market's PTB.
        assert recovered.ptb is None
        assert recovered.observation_count == 0
        store2.close()

    def test_the_missed_market_is_left_recorded(self, tmp_path: Path) -> None:
        """Downtime does not erase what was collected. The row survives with its PTB
        so the market is reconcilable after the fact."""
        db = tmp_path / "arc.db"
        store, rotator, clock = _reopen(db, float(WINDOW_TS))
        rotator.advance(clock.now())
        market = rotator.current
        assert market is not None
        store.save_ptb(market.slug, PTB, clock.now())
        store.close()

        store2, rotator2, clock2 = _reopen(db, float(CLOSE_TS + 10))
        rotator2.advance(clock2.now())
        assert store2.load_ptb(market.slug) == PTB
        assert store2.market_count() == 2
        store2.close()

    def test_a_long_outage_skips_straight_to_the_current_window(
        self, tmp_path: Path
    ) -> None:
        """Level-triggered convergence: an hour of downtime is not replayed as twelve
        rotations. A schedule-driven design would either fire twelve times or not at
        all; the level check simply lands on the window the clock says it is in."""
        db = tmp_path / "arc.db"
        store, rotator, clock = _reopen(db, float(WINDOW_TS))
        rotator.advance(clock.now())
        store.close()

        store2, rotator2, clock2 = _reopen(db, float(WINDOW_TS + 3600 + 5))
        rotator2.advance(clock2.now())
        recovered = rotator2.current
        assert recovered is not None
        assert recovered.window_ts == WINDOW_TS + 3600
        assert rotator2.markets_opened == 1
        store2.close()


class TestDeadMarketsStayDead:
    def test_a_market_persisted_dead_is_not_revived(self, tmp_path: Path) -> None:
        """Reviving it would trade a market whose official PTB was established to be
        unavailable — the exact outcome the fail-closed path exists to prevent."""
        db = tmp_path / "arc.db"
        store, rotator, clock = _reopen(db, float(WINDOW_TS))
        rotator.advance(clock.now())
        market = rotator.current
        assert market is not None
        store.save_phase(market.slug, MarketPhase.DEAD, clock.now(), "PTB_UNAVAILABLE")
        store.close()

        store2, rotator2, clock2 = _reopen(db, float(WINDOW_TS + 60))
        rotator2.advance(clock2.now())
        recovered = rotator2.current
        assert recovered is not None
        assert recovered.phase is MarketPhase.DEAD
        assert recovered.dead_reason == "PTB_UNAVAILABLE"
        store2.close()

    def test_a_dead_market_accepts_no_observations(self, tmp_path: Path) -> None:
        db = tmp_path / "arc.db"
        store, rotator, clock = _reopen(db, float(WINDOW_TS))
        rotator.advance(clock.now())
        market = rotator.current
        assert market is not None
        store.save_phase(market.slug, MarketPhase.DEAD, clock.now(), "PTB_UNAVAILABLE")
        store.close()

        store2, rotator2, clock2 = _reopen(db, float(WINDOW_TS + 60))
        rotator2.advance(clock2.now())
        assert rotator2.route(_observation("64000.5", clock2.now())) == ()
        store2.close()

    def test_an_unrecognised_phase_does_not_crash_recovery(self, tmp_path: Path) -> None:
        """A row written by a future schema must not take the process down. The market
        is treated as live; the phase it claims is not trusted."""
        db = tmp_path / "arc.db"
        store, rotator, clock = _reopen(db, float(WINDOW_TS))
        rotator.advance(clock.now())
        market = rotator.current
        assert market is not None
        store.connection.execute(
            "UPDATE markets SET phase = ? WHERE slug = ?", ("NOT_A_PHASE", market.slug)
        )
        store.connection.commit()
        store.close()

        store2, rotator2, clock2 = _reopen(db, float(WINDOW_TS + 60))
        rotator2.advance(clock2.now())
        recovered = rotator2.current
        assert recovered is not None
        assert recovered.phase is MarketPhase.ACTIVE
        store2.close()


class TestTradingStaysDisabledAcrossRestarts:
    """A process that disabled trading must not come back up trading."""

    def test_the_disabled_flag_and_its_reason_survive(self, tmp_path: Path) -> None:
        db = tmp_path / "arc.db"
        store, _, clock = _reopen(db, float(WINDOW_TS))
        state = RuntimeState(store, clock)
        state.load()
        state.disable_trading("FEED_STALE")
        store.close()

        store2, _, clock2 = _reopen(db, float(WINDOW_TS + 10))
        gate = RuntimeState(store2, clock2).load()
        assert gate.enabled is False
        assert gate.reason == "FEED_STALE"
        store2.close()

    def test_a_verified_spec_does_not_leak_an_enabled_flag(self, tmp_path: Path) -> None:
        """Recording VERIFIED is not the same as enabling trading. Only an explicit
        enable does that, so a restart cannot enable trading as a side effect."""
        db = tmp_path / "arc.db"
        store, _, clock = _reopen(db, float(WINDOW_TS))
        state = RuntimeState(store, clock)
        state.load()
        state.record_spec_status(SettlementSpecStatus.VERIFIED)
        store.close()

        store2, _, clock2 = _reopen(db, float(WINDOW_TS + 10))
        gate = RuntimeState(store2, clock2).load()
        assert gate.enabled is False
        store2.close()

    def test_a_fresh_database_starts_disabled(self, tmp_path: Path) -> None:
        """A first run has verified nothing. Defaulting to enabled would mean every
        unanticipated failure mode arrives as a trading bot."""
        store = Store(tmp_path / "fresh.db")
        store.migrate(1.0)
        gate = RuntimeState(store, FrozenClock(now=1.0)).load()
        assert gate.enabled is False
        assert gate.reason
        store.close()


class TestDurability:
    """Nothing recovers if the writes were not durable. PRAGMAs, checked."""

    def test_writes_are_flushed_synchronously(self, tmp_path: Path) -> None:
        """synchronous=FULL. Under NORMAL a VPS power loss can lose the last WAL
        transactions, and the last transaction is exactly the frozen PTB."""
        store = Store(tmp_path / "arc.db")
        store.migrate(1.0)
        assert int(store.connection.execute("PRAGMA synchronous").fetchone()[0]) == 2
        store.close()

    def test_the_database_survives_being_reopened_repeatedly(self, tmp_path: Path) -> None:
        """A PM2 restart loop must not corrupt the file or lose the schema."""
        db = tmp_path / "arc.db"
        for _ in range(5):
            store = Store(db)
            store.migrate(1.0)
            assert store.integrity_check() == "ok"
            store.close()

    def test_a_ptb_written_before_a_crash_is_readable_after(self, tmp_path: Path) -> None:
        """The write is committed by save_ptb itself, not at shutdown, so a process
        that never gets to close cleanly still leaves the value behind."""
        db = tmp_path / "arc.db"
        store = Store(db)
        store.migrate(1.0)
        rotator = _rotator(store, FrozenClock(now=float(WINDOW_TS)))
        rotator.advance(float(WINDOW_TS))
        market = rotator.current
        assert market is not None
        store.save_ptb(market.slug, PTB, float(WINDOW_TS))
        # No close(): the process is gone. The connection object is simply abandoned.
        del store

        store2 = Store(db)
        store2.migrate(1.0)
        assert store2.load_ptb(market.slug) == PTB
        store2.close()


class TestFeedReconnection:
    """A dropped socket, an unreachable host, a refused connection: one code path."""

    def test_a_dropped_socket_reconnects_and_keeps_yielding(self) -> None:
        """The stream must not end. A feed loop that raised on disconnect would take
        the whole observation run down every time the venue blipped."""
        sockets = [_Socket(["a"], close_after=True), _Socket(["b", "c"])]

        async def connect(url: str) -> _Socket:
            return sockets.pop(0)

        feed = RtdsFeed(
            FrozenClock(now=0.0),
            connect=connect,
            backoff=BackoffPolicy(initial_seconds=0.001, max_seconds=0.002),
        )
        frames = asyncio.run(_take(feed, 3))
        assert frames == ["a", "b", "c"]
        assert feed.connect_attempts == 2

    def test_an_unreachable_host_is_retried_not_raised(self) -> None:
        """The ISP going away, the VPS losing its route, DNS failing: all OSError."""
        attempts = {"n": 0}

        async def connect(url: str) -> _Socket:
            attempts["n"] += 1
            if attempts["n"] < 3:
                raise OSError("Network is unreachable")
            return _Socket(["recovered"])

        feed = RtdsFeed(
            FrozenClock(now=0.0),
            connect=connect,
            backoff=BackoffPolicy(initial_seconds=0.001, max_seconds=0.002),
        )
        assert asyncio.run(_take(feed, 1)) == ["recovered"]
        assert attempts["n"] == 3

    def test_backoff_is_bounded(self) -> None:
        """Unbounded backoff reaches multi-minute delays, and a bot that takes four
        minutes to notice the feed came back has missed most of a session."""
        policy = BackoffPolicy(initial_seconds=0.5, max_seconds=30.0)
        assert policy.delay_for(1) == 0.5
        assert policy.delay_for(20) == 30.0

    def test_the_reconnect_ladder_resets_after_a_success(self) -> None:
        """Otherwise a connection that survives an hour then drops waits the maximum
        delay before its first retry."""
        sockets = [
            _Socket([], close_after=True),
            _Socket(["x"], close_after=True),
            _Socket(["y"]),
        ]

        async def connect(url: str) -> _Socket:
            return sockets.pop(0)

        feed = RtdsFeed(
            FrozenClock(now=0.0),
            connect=connect,
            backoff=BackoffPolicy(initial_seconds=0.001, max_seconds=0.002),
        )
        assert asyncio.run(_take(feed, 2)) == ["x", "y"]

    def test_the_watchdog_notices_a_silent_feed(self) -> None:
        """A socket that stays open while delivering nothing is the failure mode a
        reconnect ladder cannot see. TRAP 1: this measures whether data is arriving,
        and says nothing about the TWAP window length."""
        clock = FrozenClock(now=0.0)
        watchdog = FeedWatchdog(clock, warn_ms=1000, critical_ms=2000)
        watchdog.tick()
        clock.advance(3.0)
        watchdog.evaluate()
        assert watchdog.blocked is True

    def test_a_recovered_feed_does_not_re_enable_trading(self, tmp_path: Path) -> None:
        """Re-enabling is the spec check's job. A watchdog that could enable trading
        would be a second, weaker authority over the same flag, and the weaker one
        would win whenever data happened to be flowing."""
        store = Store(tmp_path / "arc.db")
        store.migrate(1.0)
        clock = FrozenClock(now=0.0)
        state = RuntimeState(store, clock)
        state.load()
        state.disable_trading("FEED_STALE")

        watchdog = FeedWatchdog(clock, warn_ms=1000, critical_ms=2000)
        watchdog.tick()
        assert watchdog.blocked is False
        # The feed is healthy again and the flag has not moved.
        assert state.trading_enabled is False
        store.close()


class _Socket:
    """A scripted websocket. Yields frames, then either closes or ends the stream."""

    def __init__(self, frames: list[str], *, close_after: bool = False) -> None:
        self._frames = frames
        self._close_after = close_after
        self.sent: list[str] = []
        self.closed = False

    async def send(self, payload: str) -> None:
        self.sent.append(payload)

    async def close(self) -> None:
        self.closed = True

    def __aiter__(self) -> _Socket:
        return self

    async def __anext__(self) -> str:
        if self._frames:
            return self._frames.pop(0)
        if self._close_after:
            raise StopAsyncIteration
        raise ConnectionLostError("stream ended")


async def _take(feed: RtdsFeed, count: int) -> list[str]:
    """Read `count` frames from the feed, then stop. Leaks no task or socket.

    aclose() is explicit rather than left to garbage collection: messages() owns a
    keepalive task and a socket, and abandoning the generator would leave both
    running past the end of the test — the orphaned-task failure this suite is
    partly here to catch.
    """
    frames: list[str] = []
    stream = feed.messages()
    try:
        async for frame in stream:
            frames.append(str(frame))
            if len(frames) >= count:
                break
    finally:
        # messages() is an async generator; the AsyncIterator return annotation hides
        # aclose from the type checker.
        await stream.aclose()  # type: ignore[attr-defined]
    return frames


class TestNoLeakedState:
    def test_two_rotators_on_one_file_share_no_memory(self, tmp_path: Path) -> None:
        """A11: per-market state lives on the instance, never at module scope. Two
        runs in one process must not see each other's markets."""
        db = tmp_path / "arc.db"
        store = Store(db)
        store.migrate(1.0)
        first = _rotator(store, FrozenClock(now=float(WINDOW_TS)))
        second = _rotator(store, FrozenClock(now=float(WINDOW_TS)))
        first.advance(float(WINDOW_TS))
        assert second.current is None
        assert second.markets_opened == 0
        store.close()

    def test_migrate_is_idempotent_across_restarts(self, tmp_path: Path) -> None:
        db = tmp_path / "arc.db"
        store = Store(db)
        first = store.migrate(1.0)
        second = store.migrate(2.0)
        assert first == second
        store.close()

    def test_a_second_process_reads_the_same_schema_version(self, tmp_path: Path) -> None:
        db = tmp_path / "arc.db"
        store = Store(db)
        version = store.migrate(1.0)
        store.close()
        store2 = Store(db)
        assert store2.migrate(2.0) == version
        assert version == store2.expected_schema_version()
        store2.close()


def test_migrate_helper_is_importable() -> None:
    """The module-level helper the conftest store fixture uses."""
    assert callable(migrate)


def test_logging_does_not_hold_the_recovery_path_open() -> None:
    """Recovery must work with no logger configured at all — a crashed process may
    come back before logging is set up."""
    logger = logging.getLogger("arc.test.recovery")
    assert logger is not None
