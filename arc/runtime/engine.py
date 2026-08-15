"""The unified runtime. TWO modes, and nothing else: V1 paper, V2 live.

    V1   the COMPLETE pipeline with PaperExecutor
    V2   the COMPLETE pipeline with LiveExecutor

There is no third mode. V1 is not a lightweight simulator and not an observation
run: market engine, MAJORITY engine, risk engine, limit order engine, fills,
reprice, sweep, reconcile and recovery all execute identically. The ONLY
component that differs between the two is the executor, which is why this file
selects it once at construction and nothing below that line branches on mode. A
second runtime path is a second set of behaviour to keep in sync, and the paper
evidence would stop being evidence about the live run.

Startup order is A8's, and the order matters:

    1  load config and validate the fatal invariants     (a bad config DOES refuse)
    2  open SQLite, migrate, reconcile                   (the caller does this)
    3  recovery, once                                    (before any submission)
    4  connect the feeds
    5  automatic settlement-spec verification

THE PROCESS ALWAYS STARTS past step 1. A feed that will not connect, an
unverifiable spec, a stale watchdog — none of these exit. They set
`trading_enabled = False` with a recorded reason and everything else keeps
running: feeds retrying, TWAP accumulating, markets rotating, observations
persisted, dashboard served.

TWO GATES, both required before an order exists (A19/Q1):

    trading_enabled   the SYSTEM gate. Set by ARC only. The operator can never
                      override it.
    execution_armed   the OPERATOR gate. Start/Stop Trading only. FALSE after
                      every startup, never persisted.

Runtime running is not trading running. Disarming stops NEW submissions and
nothing else — feeds, TWAP, PTB observation, recovery and the socket all keep
going, because a kill switch that also tears down the observation stack leaves
the operator blind at exactly the moment they reached for it.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import uuid
from collections import deque
from collections.abc import Awaitable
from dataclasses import dataclass, field, replace
from decimal import Decimal
from time import perf_counter
from typing import Any, Final, Protocol, TextIO

import polymarket

from arc.api.app import check_bind
from arc.api.app import serve as serve_dashboard
from arc.buildinfo import git_commit
from arc.clock import Clock, DriftMonitor, DriftStatus
from arc.config import Settings
from arc.domain.enums import Direction, MarketPhase, Mode, Outcome, SettlementSpecStatus
from arc.domain.health import RuntimeHealth
from arc.domain.models import MarketInstance, Settlement
from arc.domain.money import dec_str, to_decimal
from arc.domain.timing import (
    MARKET_DURATION_SECONDS,
    SETTLEMENT_WINDOW_SECONDS,
    slug_for,
)
from arc.errors import ArcError, ConfigInvariantError, FeedError, ObservationRejectedError
from arc.execution.fill_engine import FillEngine
from arc.execution.protocol import Executor
from arc.execution.ratelimit import TokenBucket
from arc.execution.reconcile import Reconciler
from arc.execution.reprice import RepricePolicy, Repricer
from arc.execution.submit import Submitter
from arc.execution.sweep import Sweeper
from arc.execution.v1_paper import PaperExecutor
from arc.execution.v2_live import LiveExecutor
from arc.execution.wallet import (
    STATUS_DISCONNECTED as _WALLET_DISCONNECTED,
)
from arc.execution.wallet import WalletReader, WalletSnapshot, build_wallet
from arc.logging_setup import attach_session_id, log_event
from arc.majority.config import MAJORITY_ENGINE
from arc.majority.engine import MajorityEngine
from arc.majority.state import MajorityMarketState
from arc.market.discovery import MarketDiscovery
from arc.market.providers import TwapProvider
from arc.market.ptb import (
    PreviousClosePtbCache,
    PtbResolution,
    freeze_ptb_for,
    resolve_ptb,
)
from arc.market.rotation import MarketRotator
from arc.market.settlement_feed import SettlementTwapCollector
from arc.market.spec_check import SpecChecker
from arc.market.validation import ObservationValidator
from arc.market.watchdog import FeedWatchdog
from arc.notify.telegram import TelegramNotifier, category_settings
from arc.risk.engine import GATE_IDS, GATE_ORDER, RiskContext, RiskEngine, RiskVerdict
from arc.risk.limits import limits_from_trading
from arc.runtime.events import EventHub, attach
from arc.runtime.recovery import RecoveryReport, RecoveryRunner
from arc.runtime.state import RuntimeState
from arc.storage.store import Store

__all__ = [
    "EXPECTED_SYMBOL",
    "SUPERVISOR_FAILED",
    "SUPERVISOR_READY",
    "SUPERVISOR_STARTING",
    "SUPERVISOR_STATES",
    "SUPERVISOR_STOPPED",
    "SUPERVISOR_STOPPING",
    "ArcRuntime",
    "BookClient",
    "RuntimeStats",
    "RuntimeStatus",
    "TokenCache",
    "build_book_client",
    "run_arc",
]

# The symbol the price stream reports for the pair these markets settle on.
EXPECTED_SYMBOL: Final[str] = "BTC/USD"

# How often the level-triggered pass runs. Fine enough that a boundary is noticed
# within a fraction of a second, coarse enough to cost nothing. Not a schedule:
# every pass compares the clock against the boundary from scratch (A12).
_TICK_SECONDS: Final[float] = 0.2

# runtime_state key for the restart counter.
_RESTART_KEY: Final[str] = "restart_count"

# How often an unresolved PTB is retried. The venue publishes a market's
# `eventMetadata` roughly 25 seconds after it closes — i.e. roughly 25 seconds into
# the NEXT market's life — so a market that opens without a PTB is not a dead
# market, it is a market whose official opening reference has not been written yet.
_PTB_RETRY_SECONDS: Final[float] = 5.0

# One day, for the daily-loss gate's window.
_DAY_SECONDS: Final[float] = 86400.0

# How long past close a market waits before settlement is written. The collector
# needs observations all the way to the close instant; a few seconds of grace let
# the last frames land before the outcome is computed. No grace means a race
# between the last observation and the settlement pass, and the loser of that race
# is the TWAP the outcome is decided on.
_SETTLE_GRACE_SECONDS: Final[float] = 5.0

# How often the official CLOB book is re-read, and how long a read stays usable.
#
# The decision pass is synchronous and runs every tick, so it cannot itself await a
# venue call; the book is refreshed by the loop and read from the cache. The age
# limit is what keeps that safe — past it the quote is reported as absent and the
# window skips with NO_QUOTE, because sizing against a price that has already moved
# is the exact failure the quote gate exists to prevent. Two refresh intervals of
# slack, so one dropped request does not skip a window.
_BOOK_REFRESH_SECONDS: Final[float] = 0.5
_BOOK_MAX_AGE_SECONDS: Final[float] = 2.0

# The balance gate's input, on its own clock. Far slower than the book because a
# balance only moves when ARC itself trades or the operator funds the account, and
# a venue account call per tick would spend the rate limit that submissions need.
_WALLET_REFRESH_SECONDS: Final[float] = 15.0

# How many health transitions are kept for debugging. Bounded for the same reason
# the Signal Tank is: a 24x7 process with an unbounded list is a process that grows
# all week. Two hundred transitions is far more than one intermittent fault needs
# and still nothing in memory terms.
_HEALTH_HISTORY: Final[int] = 200

# Supervisor LIFECYCLE states. Deliberately not RuntimeStatus values: that enum is
# the trading runtime's own status and is a closed set of five. These describe the
# object that owns the runtime, and belong only on the Systems page.
SUPERVISOR_READY: Final[str] = "READY"
SUPERVISOR_STARTING: Final[str] = "STARTING"
SUPERVISOR_STOPPING: Final[str] = "STOPPING"
SUPERVISOR_FAILED: Final[str] = "FAILED"
# The idle state, between runs. Named rather than folded into one of the four
# above because an inert runtime is neither being torn down nor broken, and
# labelling it STOPPING or FAILED would put a fault on the Systems page every
# time the operator stopped cleanly.
SUPERVISOR_STOPPED: Final[str] = "STOPPED"
SUPERVISOR_STATES: Final[tuple[str, ...]] = (
    SUPERVISOR_STOPPED,
    SUPERVISOR_STARTING,
    SUPERVISOR_READY,
    SUPERVISOR_STOPPING,
    SUPERVISOR_FAILED,
)

_ZERO: Final[Decimal] = Decimal("0")

# Outcome labels the venue uses for these markets, lowercased. Matched rather than
# positional: `clobTokenIds` carries no side information, and a token id taken by
# index would place a real order on the opposite outcome while looking entirely
# healthy. An unmatched label resolves to nothing and refuses the submission.
_UP_LABELS: Final[frozenset[str]] = frozenset({"up", "yes"})
_DOWN_LABELS: Final[frozenset[str]] = frozenset({"down", "no"})

# The two providers. One or the other, never both (A3).
_PROVIDERS: Final[frozenset[str]] = frozenset({"RTDS", "CHAINLINK"})


class TimedRiskEngine(RiskEngine):
    """The risk engine, with a stopwatch around it. Verdicts are unchanged.

    The measurement lives here and not in the Decision Engine because A0 forbids
    that layer a clock of any kind, and a diagnostic is not worth a hole in the
    rule that keeps decisions reproducible. Nothing reads these numbers to decide
    anything: a gate that suddenly costs milliseconds is a fault to find, and the
    alternative to measuring it is guessing at it afterwards.
    """

    __slots__ = ("last_ms", "max_ms")

    def __init__(self) -> None:
        self.last_ms = 0.0
        self.max_ms = 0.0

    def evaluate(self, context: RiskContext) -> RiskVerdict:
        started = perf_counter()
        try:
            return super().evaluate(context)
        finally:
            self.last_ms = (perf_counter() - started) * 1000.0
            self.max_ms = max(self.max_ms, self.last_ms)


def _verdict(ok: bool) -> str:
    return "PASS" if ok else "FAIL"


def _health_line(health: RuntimeHealth) -> str:
    """One line describing a health snapshot, for the transition history.

    Only the fields an operator debugs an intermittent fault with. The full object
    is not kept: two hundred whole snapshots is a memory profile, and the fields
    left out are the ones that never move within one run.
    """
    return (
        f"enabled={health.trading_enabled} armed={health.execution_armed} "
        f"paused={health.paused} healthy={health.healthy} "
        f"feed_blocked={health.feed_blocked} drift={health.clock_drift_ms:.0f}ms "
        f"supervisor={health.supervisor_ready} wallet={health.wallet_status or '-'} "
        f"orphans={len(health.orphan_orders)} positions={health.open_positions}"
    )


class BookClient(Protocol):
    """Official public market data: the CLOB book and market metadata.

    Both modes get one. The book is public data and needs no credential, so V1
    reads exactly what V2 reads and the two adapters differ only in what they do
    with the price. Declared as a Protocol so the tests can supply a recorded book
    without a network, and so nothing here depends on the secure client.
    """

    def get_order_book(self, *, token_id: str) -> Awaitable[Any]: ...

    def get_market(self, *, slug: str) -> Awaitable[Any]: ...


def build_book_client(settings: Settings) -> polymarket.AsyncPublicClient:
    """The official public client, on the configured environment.

    Unauthenticated by construction: an idle or paper runtime must never hold a
    signing key, and the book does not require one.
    """
    return polymarket.AsyncPublicClient(environment=_environment(settings.env))


class RuntimeStatus:
    """The complete set of runtime status values. Nothing else exists (Q4)."""

    STOPPED: Final[str] = "STOPPED"
    STARTING: Final[str] = "STARTING"
    RUNNING_V1: Final[str] = "RUNNING (V1)"
    RUNNING_V2: Final[str] = "RUNNING (V2)"
    STOPPING: Final[str] = "STOPPING"

    @staticmethod
    def running_for(mode: Mode) -> str:
        return RuntimeStatus.RUNNING_V2 if mode is Mode.V2 else RuntimeStatus.RUNNING_V1


@dataclass(slots=True)
class RuntimeStats:
    """What the run did. Displayed; feeds no decision."""

    markets_processed: int = 0
    ptb_frozen: int = 0
    ptb_unavailable: int = 0
    observations_accepted: int = 0
    observations_rejected: int = 0
    settlement_samples: int = 0
    settlement_stream_found: bool = False
    reconnects: int = 0
    # Sockets that were up and went down, and recovery sequences that completed.
    # Both are separate from `reconnects`: a reconnect ladder can attempt many
    # times against one drop, and the operator asking "how often did the feed
    # actually go away" is not asking how many times it retried.
    disconnects: int = 0
    recoveries: int = 0
    orders_submitted: int = 0
    orders_repriced: int = 0
    fills_recorded: int = 0
    per_market_ticks: dict[str, int] = field(default_factory=dict)

    def observed_cadence_ms(self, elapsed_seconds: float) -> float | None:
        """Mean gap between accepted observations, for the report only.

        Says nothing about the settlement TWAP window length and is never used to
        infer or check it.
        """
        if self.observations_accepted < 2 or elapsed_seconds <= 0:
            return None
        return elapsed_seconds * 1000.0 / self.observations_accepted


class TokenCache:
    """`(slug, direction) -> CLOB token id`, populated from official metadata.

    The Executor's TokenResolver is synchronous and the lookup is a network call,
    so ids are fetched once when a market opens and read from here afterwards. A
    miss raises rather than returning a plausible id: submitting against a guessed
    token places a real order on the opposite outcome and every field on it looks
    correct.
    """

    __slots__ = ("_by_slug",)

    def __init__(self) -> None:
        self._by_slug: dict[str, dict[Direction, str]] = {}

    def put(self, slug: str, direction: Direction, token_id: str) -> None:
        self._by_slug.setdefault(slug, {})[direction] = token_id

    def drop(self, slug: str) -> None:
        self._by_slug.pop(slug, None)

    def known(self, slug: str) -> bool:
        return len(self._by_slug.get(slug, {})) == 2

    def __call__(self, market_slug: str, direction: Direction) -> str:
        token_id = self._by_slug.get(market_slug, {}).get(direction)
        if token_id is None:
            raise ArcError(
                f"no official token id for {market_slug} {direction.value}; "
                "refusing to trade a token id that was not published by the venue"
            )
        return token_id


class ArcRuntime:
    """One run. Owns every engine, in one process, for one mode.

    Everything mutable is an attribute of this object. A second run in the same
    process is a second instance with its own markets, accumulators and validator
    history (A11).
    """

    __slots__ = (
        "_book",
        "_book_client",
        "_bucket",
        "_clock",
        "_discovery",
        "_drift",
        "_executor",
        "_feed",
        "_fills",
        "_health_history",
        "_health_prev",
        "_health_revision",
        "_hub",
        "_logger",
        "_majority",
        "_majority_repricers",
        "_majority_submitter",
        "_next_book_read",
        "_next_ptb_attempt",
        "_next_wallet_read",
        "_notifier",
        "_out",
        "_paused",
        "_previous_close",
        "_reconciler",
        "_recovery",
        "_risk",
        "_runtime",
        "_settings",
        "_settlement",
        "_spec",
        "_store",
        "_sweeper",
        "_validator",
        "_venue_client",
        "_wallet",
        "_wallet_available",
        "_wallet_refreshed_at",
        "_wallet_status",
        "_watchdog",
        "mode",
        "restart_count",
        "rotator",
        "runtime_session_id",
        "start_reason",
        "started_at",
        "stats",
        "status",
        "supervisor_detail",
        "supervisor_ready",
        "supervisor_state",
        "tokens",
    )

    def __init__(
        self,
        *,
        settings: Settings,
        store: Store,
        clock: Clock,
        runtime: RuntimeState,
        discovery: MarketDiscovery,
        feed: TwapProvider,
        executor: Executor,
        out: TextIO,
        venue_client: polymarket.AsyncSecureClient | None = None,
        book_client: BookClient | None = None,
        logger: logging.Logger | None = None,
        runtime_session_id: str = "",
        start_reason: str = "manual",
    ) -> None:
        trading = settings.trading
        self._settings = settings
        self._store = store
        self._clock = clock
        self._runtime = runtime
        # First-run seeding. The CLI seeds the settings table on boot when it
        # loaded from .env (cli.py, `seeded_from_env`), but any construction that
        # bypasses the CLI — a test, an embedding host — leaves the table empty,
        # and the /settings handler merges over whatever the store HOLDS. The
        # complete row must exist before the first save, or that save becomes the
        # write that first materialises the other engine's values. An empty table
        # is unambiguous: nothing stored means nothing can be overwritten.
        if not store.load_settings():
            store.save_settings(settings.as_storage_dict(), clock.now())
        self._discovery = discovery
        self._feed = feed
        self._executor = executor
        self._venue_client = venue_client
        # The official CLOB book, read once per pass by the runtime and handed to
        # whichever adapter is running. One pipeline, not one per adapter: V1 must
        # size against the same book V2 would, or the paper run stops being
        # evidence about the live one.
        self._book_client = book_client
        self._book: dict[tuple[str, Direction], tuple[Decimal, float]] = {}
        self._next_book_read: float = 0.0
        self._out = out
        # Supervisor supplies this for live runtimes; the fallback keeps direct test
        # construction traceable without creating a second lifecycle owner.
        self.runtime_session_id = runtime_session_id or uuid.uuid4().hex
        self.start_reason = start_reason
        self._logger = logger
        # A filter, deliberately not a LoggerAdapter. An adapter's process() replaces
        # the caller's `extra` wholesale, which would drop `arc_detail` from every
        # log_event in the codebase — silently blanking the reason text on every
        # denial and every PTB failure. A filter adds the field and touches nothing.
        if logger is not None:
            attach_session_id(logger, self.runtime_session_id)
        # Pause is the operator's "hold new submissions" and is deliberately
        # separate from disarming: pausing must not clear the arm state, or resuming
        # would silently require a second confirmation the operator did not expect.
        self._paused = False
        self._hub = EventHub()
        self._hub.runtime_session_id = self.runtime_session_id
        self.mode = executor.mode
        self.status = RuntimeStatus.STOPPED
        self.started_at = 0.0
        # Persisted across restarts, so the System page can show "restarted 14
        # times" — the number that tells the operator PM2 is looping on a crash
        # rather than that the process has been up all week.
        self.restart_count = int(store.get_runtime_state(_RESTART_KEY) or 0)
        self.stats = RuntimeStats()

        self._validator = ObservationValidator()
        self._watchdog = FeedWatchdog(
            clock,
            warn_ms=trading.feed_stale_warn_ms,
            critical_ms=trading.feed_stale_critical_ms,
        )
        self._drift = DriftMonitor(
            warn_ms=trading.clock_drift_warn_ms,
            critical_ms=trading.clock_drift_critical_ms,
        )
        self._spec = SpecChecker(logger=logger)
        self._settlement: dict[str, SettlementTwapCollector] = {}
        # The L2 PTB source: the venue's published finalPrice for the previous
        # market. Instance state, never module state — two runs in one process must
        # not share a cached price (A11).
        self._previous_close = PreviousClosePtbCache()
        self._next_ptb_attempt: float = 0.0
        self.tokens = TokenCache()
        # Recovery's own report, kept so the dashboard can show reconciliation
        # progress. None until recovery runs: "not yet run" and "ran and found
        # nothing" are different facts, and a restart is exactly when the operator
        # needs to know which one they are looking at.
        self._recovery: RecoveryReport | None = None
        self._wallet = build_wallet(executor.mode, store, venue_client)
        # Last reported wallet status, so a transition is logged once instead of on
        # every poll. An event per poll would bury the one that mattered.
        self._wallet_status = ""
        # The last balance gate 19 evaluated, and when the next read is due. Read on
        # the loop rather than inside the gate: the decision pass is synchronous and
        # a gate that awaited a venue call would put a round trip inside the freeze.
        # None means no official source has published one, which is V1's permanent
        # state and gate 19 treats as "no opinion" rather than as zero.
        self._wallet_available: Decimal | None = None
        self._next_wallet_read: float = 0.0
        # 0.0 = never read. Distinct from "read and found nothing", which is a
        # completed read with available_balance None.
        self._wallet_refreshed_at: float = 0.0
        # The supervisor's own verdict on this runtime, pushed down rather than
        # pulled: the runtime must not hold a reference to the object that owns it,
        # or a stopped runtime keeps its supervisor alive. Defaults READY so a
        # runtime nobody supervises — every test, and `arc run` before the
        # supervisor attaches — is not refused by a gate about the supervisor.
        self.supervisor_ready = True
        self.supervisor_detail = ""
        self.supervisor_state = SUPERVISOR_READY
        # Health revision and its history. The revision is bumped only when a field
        # actually changes, so the dashboard can redraw on a change rather than on
        # every frame; the history is the last _HEALTH_HISTORY transitions, kept for
        # the intermittent fault that is gone by the time anybody looks.
        self._health_revision = 0
        self._health_prev: tuple[object, ...] = ()
        self._health_history: deque[tuple[float, int, str]] = deque(maxlen=_HEALTH_HISTORY)
        # Notification only. Constructed unconditionally and inert without a token, so
        # there is no code path that exists only when Telegram is configured — the
        # untested path is the one that breaks the night it is first needed.
        self._notifier = TelegramNotifier(
            token=settings.env.telegram_bot_token.get_secret_value(),
            chat_id=settings.env.telegram_chat_id,
            enabled=settings.env.telegram_enabled,
            thread_id=settings.env.telegram_thread_id,
            flags=category_settings(store.load_settings()),
            logger=logger,
        )

        # ── execution half ───────────────────────────────────────────────────
        # MAJORITY is the only trading engine. The TWAP trading engine — its
        # submitter, repricer, decision engine and window engine — has been
        # removed; TWAP survives only as a data source feeding MAJORITY.
        self._bucket = TokenBucket(
            sustained=trading.outbound_rate_sustained,
            burst=trading.outbound_rate_burst,
            now=clock.now(),
        )
        self._fills = FillEngine(store, executor, logger=logger)
        # Per-window MAJORITY repricer (spec §11 price retry). Each window has
        # its own entry band, so the repricer must follow the band of the WINDOW
        # the order belongs to — a foreign-band repricer on a MAJORITY order would
        # move the order to a price outside the band the operator set for MAJORITY,
        # and the gates that approved the order would no longer cover the resting
        # price. Built as a dict keyed by window so the engine looks up the policy
        # by the order's offset in O(1).
        #
        # The §11 switch is the whole gate: with price retry OFF there are NO
        # repricers and `_reprice_open` finds nothing to look up, so a resting
        # MAJORITY order stays exactly where it was placed.
        #
        # Final spec §20/§22: +1/-1 repricing applies ONLY while the
        # trigger/target switch is OFF. With the switch ON the order is placed
        # at the configured target price and must rest there — never walked to
        # $0.94 or $0.92. The two conditions together are the gate; a repricer
        # built under either one alone would violate the other rule.
        self._majority_repricers: dict[int, Repricer] = {
            window.execution_window_seconds: Repricer(
                store,
                executor,
                RepricePolicy(
                    band_min=window.entry_price_min,
                    band_max=window.entry_price_max,
                    tick=trading.tick_size,
                ),
                bucket=self._bucket,
                # Final spec §20: same-price attempts before the first reprice,
                # configurable 5-10 and validated at configuration time.
                pre_reprice_attempts=settings.majority.price_retry_attempts,
                logger=logger,
            )
            for window in settings.majority.tradable_windows
        } if (
            settings.majority.price_retry_enabled
            and not settings.majority.trigger_limit_enabled
        ) else {}
        self._sweeper = Sweeper(store, executor, logger=logger)
        self._reconciler = Reconciler(store, executor, logger=logger)

        # ── MAJORITY half ────────────────────────────────────────────────────
        # Constructed with engine=MAJORITY so every order it derives carries the
        # MAJORITY prefix and the MAJORITY engine column. The historical TWAP
        # rows keep their empty prefix, so no existing order id changes.
        #
        # `minimum` is the MINIMUM share count across the configured windows. The
        # split is over MAJORITY's size and a foreign minimum would either reject
        # the configured size or split it into a ladder MAJORITY never asked for.
        # Each window's own builder already refused any share count below the
        # venue minimum, so this value is never under it; using the smallest
        # configured value keeps the submitter's split arithmetic permissive
        # without ever accepting a sub-minimum share count.
        majority = settings.majority
        if majority.windows_by_offset:
            _majority_min = min(w.shares for w in majority.windows_by_offset)
        else:
            _majority_min = trading.min_tradable_size
        self._majority_submitter = Submitter(
            store,
            executor,
            bucket=self._bucket,
            minimum=_majority_min,
            engine=MAJORITY_ENGINE,
            logger=logger,
        )
        # Constructed even when MAJORITY is OFF. Every entry point checks
        # `config.tradable` and returns, so an OFF engine reads no book, evaluates no
        # trigger and submits nothing — while the dashboard can still ask it for its
        # state and get an honest OFF instead of an AttributeError.
        self._majority = MajorityEngine(
            majority,
            store,
            executor,
            self._majority_submitter,
            tick_size=trading.tick_size,
            logger=logger,
        )

        # ── risk + rotation ──────────────────────────────────────────────────
        # Owned here so the Systems page can read the timings without reaching
        # through a decision layer into a gate it is not allowed to time.
        self._risk = TimedRiskEngine()
        # MAJORITY's windows are windows of the market too: the ledger, the
        # recorder and the deck all read the windows table, and a window with
        # no row there is a trade that never appears in any of them. Trading's
        # offsets plus MAJORITY's tradable offsets form the row set; the union
        # is computed here because the rotator persists it at market creation
        # and settings only change by rebuilding this runtime.
        offsets = tuple(
            sorted(
                set(trading.windows_by_priority)
                | {w.execution_window_seconds for w in majority.tradable_windows}
            )
        )
        self.rotator = MarketRotator(
            store,
            clock,
            offsets=offsets,
            logger=logger,
        )

    # ── read access for the dashboard ────────────────────────────────────────
    # Properties rather than public attributes so the API can only READ. A
    # dashboard that could reach in and set a frozen value would be the one place
    # in the process able to change a number after it was locked.

    @property
    def settings(self) -> Settings:
        return self._settings

    @property
    def store(self) -> Store:
        return self._store

    @property
    def clock(self) -> Clock:
        return self._clock

    @property
    def state(self) -> RuntimeState:
        return self._runtime

    @property
    def feed(self) -> TwapProvider:
        return self._feed

    @property
    def executor(self) -> Executor:
        return self._executor

    @property
    def watchdog(self) -> FeedWatchdog:
        return self._watchdog

    @property
    def drift(self) -> DriftMonitor:
        return self._drift

    @property
    def spec(self) -> SpecChecker:
        return self._spec

    @property
    def wallet(self) -> WalletReader:
        return self._wallet

    @property
    def recovery_report(self) -> RecoveryReport | None:
        """What the last recovery pass found. None until recovery has run."""
        return self._recovery

    @property
    def majority(self) -> MajorityEngine:
        """The MAJORITY engine. Always present, even when the engine is OFF.

        Exposed so the dashboard can render MAJORITY's own state without holding a
        second copy of it. Read-only by the same rule as every property above: a
        dashboard able to advance the sequence would be a second caller of
        `select_side`, and the side lock exists precisely because there must only
        ever be one.
        """
        return self._majority

    def majority_state_for(self, slug: str) -> MajorityMarketState | None:
        """MAJORITY's state for one market, or None when it is not tracked.

        None is honest rather than a synthesised OFF row: a market MAJORITY never
        opened and a market MAJORITY opened and switched off are different facts,
        and the deck must not print the second when the first is true.
        """
        return self._majority.state_for(slug)

    # ── gate readiness ───────────────────────────────────────────────────────

    def gate_readiness(self) -> tuple[dict[str, str], ...]:
        """Every gate's STANDING state: is there a process-wide reason it refuses?

        This is a readiness summary, not an evaluation. Nine of the nineteen gates
        answer questions about the process — trading enabled, armed, feed fresh,
        supervisor ready, wallet connected, balance sufficient — and those are
        answered here against the same health snapshot the real evaluation uses.

        The rest need a window: a trigger, a price, a size, a direction. They are
        reported PER WINDOW rather than assumed to pass, because a gate reported
        PASS on a window that does not exist is a pass ARC invented. They are
        counted as not-blocking in the summary, which is what "ready" means: the
        runtime is not standing in the way, the window decides the rest.
        """
        health = self.health()
        gate = self._runtime.gate
        limits = limits_from_trading(self._settings.trading)
        report = self._recovery
        orphans = () if report is None else report.orphans
        standing: dict[str, tuple[bool, str]] = {
            "trading_enabled": (gate.enabled, gate.reason or "enabled"),
            "execution_armed": (gate.armed, "armed" if gate.armed else "not armed"),
            "strategy_enabled": (
                self._settings.majority.enabled,
                "MAJORITY enabled" if self._settings.majority.enabled else "MAJORITY disabled",
            ),
            "loss_limits": (
                health.daily_loss_usd <= limits.max_daily_loss_usd
                and health.consecutive_losses < limits.max_consecutive_losses,
                f"{dec_str(health.daily_loss_usd)} today, "
                f"{health.consecutive_losses} consecutive",
            ),
            "feed_freshness": (not health.feed_blocked, self._watchdog.status),
            "runtime_health": (
                health.healthy and not health.clock_drift_critical,
                health.detail or f"drift {health.clock_drift_ms:.0f}ms",
            ),
            "supervisor_ready": (
                health.supervisor_ready,
                health.supervisor_detail or self.supervisor_state,
            ),
            "wallet_connected": (health.wallet_connected, health.wallet_status or "not polled"),
            "orphan_orders": (not orphans, f"{len(orphans)} orphans"),
            "available_balance": (
                True,
                "no published balance"
                if health.available_balance is None
                else f"{dec_str(health.available_balance)} available",
            ),
        }
        rows: list[dict[str, str]] = []
        for name in GATE_ORDER:
            if name not in standing:
                rows.append(
                    {"id": GATE_IDS[name], "gate": name, "state": "PER WINDOW", "detail": ""}
                )
                continue
            ok, detail = standing[name]
            rows.append(
                {
                    "id": GATE_IDS[name],
                    "gate": name,
                    "state": "PASS" if ok else "BLOCKED",
                    "detail": detail,
                }
            )
        return tuple(rows)

    def balance_detail(self, now: float) -> dict[str, Any]:
        """Gate 19's arithmetic, shown rather than only enforced.

        Required is the configured position notional — what one order will cost —
        rather than a live intent's price times size, because the deck must be able
        to answer "can the next order pay" before there is an intent to price.

        Available None means no official source published a figure. It is reported
        as None all the way to the browser rather than as zero: gate 19 treats an
        absent balance as "no opinion", and a zero on the deck would read as an
        empty account.
        """
        available = self._wallet_available
        required = self._settings.trading.position_notional_usd
        return {
            "available": None if available is None else dec_str(available),
            "required": dec_str(required),
            "difference": None if available is None else dec_str(available - required),
            "sufficient": available is None or available >= required,
            "last_refresh": self._wallet_refreshed_at or None,
            "refresh_age_ms": self.wallet_refresh_age_ms(now),
        }

    def gate_summary(self) -> dict[str, Any]:
        """`19 / 19 Gates PASS`, plus the rows behind it.

        Resolved here rather than in the payload builder, which is serialization
        only: a serializer that counted these would be a second implementation of
        what "ready" means.
        """
        rows = self.gate_readiness()
        blocked = [row for row in rows if row["state"] == "BLOCKED"]
        passing = len(rows) - len(blocked)
        return {
            "rows": list(rows),
            "total": len(rows),
            "passing": passing,
            "summary": f"{passing} / {len(rows)} Gates PASS",
            "failures": blocked,
        }

    @property
    def risk_eval_ms(self) -> float:
        """How long the last risk evaluation took. Diagnostics only."""
        return self._risk.last_ms

    @property
    def risk_eval_max_ms(self) -> float:
        """The worst risk evaluation of this run. Diagnostics only."""
        return self._risk.max_ms

    @property
    def venue_client(self) -> polymarket.AsyncSecureClient | None:
        return self._venue_client

    @property
    def hub(self) -> EventHub:
        """The Signal Tank. The API subscribes; the engines only publish."""
        return self._hub

    @property
    def notifier(self) -> TelegramNotifier:
        """Telegram. Read-only from here: it observes the hub and controls nothing."""
        return self._notifier

    async def wallet_snapshot(self, now: float) -> WalletSnapshot:
        """One wallet read, with a connection CHANGE logged as an event.

        The panel is polled once per status frame, so logging the status itself would
        produce a line per second. Only a transition is an event — and it has to be
        one, because a wallet that silently went DISCONNECTED means every balance on
        screen is the last one that was true rather than the one that is.
        """
        snapshot = await self._wallet.snapshot(now, run_start=self.started_at)
        if snapshot.status != self._wallet_status:
            previous, self._wallet_status = self._wallet_status, snapshot.status
            connected = snapshot.status != _WALLET_DISCONNECTED
            log_event(
                logging.INFO if connected else logging.WARNING,
                # The first reading is "Connected", every later one "Reconnected".
                # An operator who never saw a disconnect must not be told the
                # wallet reconnected, because that reads as an outage they missed.
                ("Wallet Connected" if not previous else "Wallet Reconnected")
                if connected
                else "Wallet Disconnected",
                f"{snapshot.provider}  {snapshot.status}",
                logger=self._logger,
            )
        return snapshot

    @property
    def paused(self) -> bool:
        return self._paused

    def settlement_twap(self, slug: str) -> Decimal | None:
        """The venue's 30s mean as collected so far. Observational only."""
        collector = self._settlement.get(slug)
        return None if collector is None else collector.settlement_twap

    # ── operator gate ────────────────────────────────────────────────────────

    def arm(self) -> None:
        """The Start Trading button. Nothing else may call this."""
        self._runtime.arm_execution()
        self._log_gate("Trading Started", "the Limit Order Engine is armed")

    def disarm(self) -> None:
        """The Stop Trading button. Stops NEW submissions and nothing else."""
        self._runtime.disarm_execution()
        self._log_gate(
            "Trading Stopped",
            "no new intents; resting orders continue to reconciliation and settlement",
        )

    def pause(self) -> None:
        """Hold new submissions without disarming. Feeds, TWAP and recovery continue."""
        self._paused = True
        self._log_gate("Trading Paused", "no new intents; resting orders are still managed")

    def resume(self) -> None:
        self._paused = False
        self._log_gate("Trading Resumed", "new intents may be created again")

    def _log_gate(self, title: str, detail: str) -> None:
        """One line per operator gate change, on the same stream as everything else.

        Through `log_event` rather than a direct hub publish, so the change lands in
        the Signal Tank, the Telegram feed and the log file together. A gate that
        moved with no line is the operator's own action becoming the one thing they
        cannot find afterwards when reconstructing why a window did not trade.
        """
        gate = self._runtime.gate
        log_event(
            logging.INFO,
            title,
            f"{detail}  (armed {gate.armed}, paused {self._paused})",
            logger=self._logger,
        )

    # ── gate inputs ──────────────────────────────────────────────────────────

    def health(self) -> RuntimeHealth:
        """One snapshot of process state for the risk gates.

        Gathered once per decision pass rather than gate by gate: nineteen gates
        each taking their own live reading would evaluate nineteen slightly
        different worlds and the verdict would depend on how long evaluation took.

        Synchronous, so nothing here may await. The two fields that need a venue —
        the wallet's status and its balance — are refreshed by the main loop into
        `_wallet_status` and `_wallet_available` and read from there. A gate that
        awaited a round trip would put it inside the decision pass, which is the
        one place in ARC that must not block.
        """
        drift = self._drift.last
        realised = self._realised_losses()
        report = self._recovery
        health = RuntimeHealth(
            trading_enabled=self._runtime.trading_enabled,
            spec_status=self._runtime.spec_status,
            execution_armed=self._runtime.execution_armed,
            paused=self._paused,
            trading_disabled_reason=self._runtime.reason,
            feed_blocked=self._watchdog.blocked,
            feed_age_ms=self._watchdog.age_ms(),
            clock_drift_critical=drift is not None and drift.status == DriftStatus.CRITICAL,
            clock_drift_ms=0.0 if drift is None else drift.offset_ms,
            healthy=self.status == RuntimeStatus.running_for(self.mode),
            detail="" if self.status == RuntimeStatus.running_for(self.mode) else self.status,
            open_positions=len(self._store.live_orders()),
            daily_loss_usd=realised[0],
            consecutive_losses=realised[1],
            supervisor_ready=self.supervisor_ready,
            supervisor_detail=self.supervisor_detail,
            # Only an ERRORED read is a disconnection. The empty string is "no
            # poll has completed yet" and PAPER is V1's permanent, correct state;
            # refusing on either would refuse the first pass of every run.
            wallet_connected=self._wallet_status != _WALLET_DISCONNECTED,
            wallet_status=self._wallet_status,
            orphan_orders=() if report is None else report.orphans,
            available_balance=self._wallet_available,
            mode=self.mode.value,
        )
        return self._revise(health)

    def _revise(self, health: RuntimeHealth) -> RuntimeHealth:
        """Stamp the revision, bumping it only when something actually changed.

        Compared field by field rather than by object identity: RuntimeHealth is
        rebuilt on every pass, so identity would report a change every time and the
        revision would be a tick counter — which is exactly the redraw-per-frame the
        revision exists to avoid. The revision itself is excluded from the
        comparison, or every bump would justify the next one.
        """
        fields = tuple(
            getattr(health, name)
            for name in health.__slots__
            if name != "health_revision"
        )
        if fields != self._health_prev:
            self._health_prev = fields
            self._health_revision += 1
            self._health_history.append(
                (self._clock.now(), self._health_revision, _health_line(health))
            )
        return replace(health, health_revision=self._health_revision)

    @property
    def health_revision(self) -> int:
        """The current revision. Bumped on change, never on a repeated frame."""
        return self._health_revision

    @property
    def health_history(self) -> tuple[tuple[float, int, str], ...]:
        """The last transitions, oldest first. Debugging only; nothing reads it."""
        return tuple(self._health_history)

    @property
    def wallet_refreshed_at(self) -> float:
        """When the balance was last read. 0.0 means never."""
        return self._wallet_refreshed_at

    def wallet_refresh_age_ms(self, now: float) -> float | None:
        """Age of the balance reading, or None when there has never been one."""
        if self._wallet_refreshed_at <= 0.0:
            return None
        return max(0.0, (now - self._wallet_refreshed_at) * 1000.0)

    def _realised_losses(self) -> tuple[Decimal, int]:
        """Today's loss magnitude and the current losing streak, from settlements.

        Read from the persisted ledger rather than from an in-memory counter so a
        restart cannot reset a breached daily limit back to zero — which would let
        the process resume trading precisely because it had just crashed.
        """
        cutoff = self._clock.now() - _DAY_SECONDS
        loss = _ZERO
        streak = 0
        counting = True
        for record in self._store.settlement_history(limit=200):
            if record.pnl < _ZERO:
                if record.settled_at >= cutoff:
                    loss -= record.pnl
                if counting:
                    streak += 1
            elif record.pnl > _ZERO:
                counting = False
        return loss, streak

    async def _refresh_books(self, now: float) -> None:
        """Re-read the official CLOB book for every live market and both sides.

        The ONE market-data path for quotes. The executor is handed the result
        rather than fetching it, so V1 and V2 differ only in what they do with the
        price — which is the whole point of the two-adapter design.

        A read that fails leaves the previous value in place to age out on its own.
        Clearing it here would turn one dropped request into a skipped window, and
        the age limit already covers the case where the failures persist.
        """
        if self._book_client is None or now < self._next_book_read:
            return
        self._next_book_read = now + _BOOK_REFRESH_SECONDS
        for market in self._live_markets():
            for direction in (Direction.UP, Direction.DOWN):
                try:
                    token_id = self.tokens(market.slug, direction)
                except ArcError:
                    # Token ids arrive from official metadata when the market
                    # opens. Not yet cached is normal for the first passes.
                    continue
                try:
                    book = await self._book_client.get_order_book(token_id=token_id)
                except Exception as exc:
                    log_event(
                        logging.WARNING,
                        "Book Unavailable",
                        f"{market.slug} {direction.value}  {exc}",
                        logger=self._logger,
                    )
                    continue
                if not book.bids:
                    continue
                # By max, not by index: a change in the venue's ordering must not
                # silently make ARC join the worst price on the book. Same rule as
                # `LiveExecutor.best_price`, for the same reason.
                best = max(level.price for level in book.bids)
                self._book[(market.slug, direction)] = (best, now)
                if isinstance(self._executor, PaperExecutor):
                    # The paper adapter's own book, so `best_price` — which the
                    # repricer and /orderbook read — answers with the live price.
                    # V2 reads the venue directly and needs nothing handed to it.
                    self._executor.quote(market.slug, direction, best)
                    # Simulate counterparty activity at the live CLOB price so
                    # resting paper orders can fill honestly.  Without this bridge
                    # the paper executor never sees a trade, every order sits until
                    # the settlement sweep cancels it, and fills are always zero.
                    # The simulated size covers the configured share count plus
                    # headroom; any excess is simply ignored by the matcher.
                    produced = self._executor.trade(
                        market.slug, best, Decimal("100"), direction=direction,
                    )
                    for fill in produced:
                        log_event(
                            logging.INFO,
                            "Paper Fill",
                            f"{market.slug} {direction.value} "
                            f"price={fill.price} size={fill.size} "
                            f"order={fill.order_id}",
                            logger=self._logger,
                        )

    async def _refresh_wallet(self, now: float) -> None:
        """Re-read the venue account so gate 19 has a current balance.

        On the loop for the same reason the book is: the decision pass is
        synchronous and a gate that awaited a venue call would put a round trip
        inside the freeze.

        A read that fails leaves `available_balance` at whatever the reader
        published — None when there is no official source, which gate 19 treats as
        "no opinion" rather than as an empty account. `wallet_snapshot` is reused
        rather than reading the wallet directly, so a connection change is logged
        exactly once from exactly one place.
        """
        if now < self._next_wallet_read:
            return
        self._next_wallet_read = now + _WALLET_REFRESH_SECONDS
        snapshot = await self.wallet_snapshot(now)
        self._wallet_available = snapshot.available_balance
        # The instant the figure above was obtained, so the deck can say how old it
        # is. A balance with no age reads as current no matter when it was read.
        self._wallet_refreshed_at = now

    def _forget_book(self, slug: str) -> None:
        for direction in Direction:
            self._book.pop((slug, direction), None)
        if isinstance(self._executor, PaperExecutor):
            self._executor.forget(slug)

    # ── PTB ──────────────────────────────────────────────────────────────────

    async def _attempt_ptb(self, now: float) -> None:
        """Obtain the official PTB for the live market. Retries until too late.

        A market that opens without a PTB is NOT yet a dead market. The venue writes
        `eventMetadata` roughly 25 seconds after a market closes, so the live
        market's official opening reference — the previous market's published
        `finalPrice` — does not exist at the instant the market opens. Marking the
        market DEAD on the first miss would kill every single market for a value
        that was about to be published.

        The retry is bounded by the market's own earliest execution window. Past
        that instant a PTB could not be used, so the market is marked DEAD with the
        reason recorded (A1 Rule 1). Nothing is estimated at any point.
        """
        market = self.rotator.current
        if market is None or market.ptb is not None or market.phase is MarketPhase.DEAD:
            return
        if now < self._next_ptb_attempt:
            return
        self._next_ptb_attempt = now + _PTB_RETRY_SECONDS

        # L2 first, and unconditionally: the previous market's finalPrice is fetched
        # even when L1 is about to succeed, because caching it is how the NEXT market
        # gets its reference. Doing it only on an L1 miss would leave the cache empty
        # in exactly the runs where it is needed.
        await self._cache_previous_close(market.window_ts)

        try:
            metadata = await self._discovery.fetch_metadata(market.slug)
        except FeedError as exc:
            self._maybe_dead(market, now, f"metadata request failed: {exc}")
            return

        resolution = resolve_ptb(
            metadata,
            window_ts=market.window_ts,
            previous_close=self._previous_close.for_window(market.window_ts),
        )
        if not resolution.available:
            self._maybe_dead(market, now, resolution.detail)
            return

        if freeze_ptb_for(market, resolution, logger=self._logger):
            assert resolution.value is not None
            self._store.save_ptb(market.slug, resolution.value, now)
            self.stats.ptb_frozen += 1
            self._print_ptb_line(market.slug, resolution)

    async def _cache_previous_close(self, window_ts: int) -> None:
        """Fetch the market that closed at `window_ts` and cache its final price.

        Silent on failure. The field is null for a market's entire life, so a fetch
        that finds nothing is the ordinary case; the caller discovers the absence by
        the PTB staying unresolved.
        """
        settled_window_ts = window_ts - MARKET_DURATION_SECONDS
        if self._previous_close.for_window(window_ts) is not None:
            return
        try:
            metadata = await self._discovery.fetch_metadata(slug_for(settled_window_ts))
        except FeedError:
            return
        entry = self._previous_close.offer(metadata, settled_window_ts=settled_window_ts)
        if entry is not None:
            log_event(
                logging.INFO,
                "PTB Cached",
                f"{entry.raw}  from settled market {entry.settled_window_ts}  "
                f"opens {entry.opens_window_ts}",
                logger=self._logger,
            )

    def _maybe_dead(self, market: MarketInstance, now: float, detail: str) -> None:
        """PTB is display-only (user directive).

        Missing PTB no longer kills the market. Log for dashboard visibility only;
        keep the market tradable regardless of PTB availability.
        """
        if now < self._ptb_deadline(market):
            return
        # PTB is display-only — never gate or kill the market on it.
        log_event(
            logging.INFO,
            "PTB Not Yet Available",
            f"{market.slug} — PTB missing but market stays active ({detail})",
            logger=self._logger,
        )
        self.stats.ptb_unavailable += 1

    def _ptb_deadline(self, market: MarketInstance) -> float:
        """The last instant a PTB could still be used: the first window's activation.

        Derived from the configured offsets rather than a constant, so a change to
        the window set moves the deadline with it instead of silently disagreeing.
        """
        offsets = self._settings.trading.windows_by_priority
        return float(market.close_ts - max(offsets)) if offsets else float(market.close_ts)

    # ── observations ─────────────────────────────────────────────────────────

    def _handle_frame(self, frame: str | bytes) -> None:
        """Validate one frame and route it. Rejections are counted, never repaired."""
        received_at = self._clock.now()
        try:
            payload = json.loads(frame)
        except ValueError:
            self.stats.observations_rejected += 1
            return

        for message in _messages_in(payload):
            self._handle_message(message, received_at)

    def _handle_message(self, message: Any, received_at: float) -> None:
        self._spec.offer(message)
        try:
            observation = self._validator.validate_payload(
                message, expected_symbol=EXPECTED_SYMBOL, received_at=received_at
            )
        except ObservationRejectedError as _exc:
            self.stats.observations_rejected += 1
            if self.stats.observations_rejected <= 10:
                _msg_str = (
                    json.dumps(message)[:200]
                    if isinstance(message, dict)
                    else str(message)[:200]
                )
                log_event(
                    logging.WARNING,
                    "Observation Rejected",
                    f"{_exc} | msg={_msg_str}",
                    logger=self._logger,
                )
            return

        self.stats.observations_accepted += 1
        self._watchdog.tick()
        # The feed carries the venue's own timestamp; comparing it to the local clock
        # is the only drift measurement available on a VPS with no other reference.
        self._drift.observe(received_at, observation.ts)

        for market in self.rotator.route(observation):
            self._store.save_observation(market.slug, observation, received_at)
            self.stats.per_market_ticks[market.slug] = (
                self.stats.per_market_ticks.get(market.slug, 0) + 1
            )
            collector = self._settlement.get(market.slug)
            if collector is not None and collector.offer(observation):
                self.stats.settlement_samples += 1
                self.stats.settlement_stream_found = True

    # ── the limit order engine ───────────────────────────────────────────────

    async def _drive_execution(self, now: float) -> None:
        """MAJORITY decision, fill, reprice. Level-triggered and idempotent.

        MAJORITY is the only engine that submits: it persists an intent and hands
        it to the submitter inside its own tick, under the same operator gates
        checked here. Nothing re-submits a persisted intent on its own — the
        removed TWAP engine's unsubmitted intents stay unsubmitted, and MAJORITY's
        are guarded by its own duplicate tracking.
        """
        # Gathered at most ONCE per pass, and only when MAJORITY can actually use
        # it. `health()` runs two SQLite reads (live orders, realised losses) and
        # this loop turns over every 200 ms, so building it unconditionally would
        # add those queries five times a second to every run — including the runs
        # where MAJORITY is OFF and nothing would ever read the result.
        #
        # One snapshot serves both live markets deliberately. Two snapshots taken a
        # few milliseconds apart could disagree on open positions or the balance,
        # and the two markets alive across a close boundary would then be gated
        # against different views of the same process.
        health: RuntimeHealth | None = None
        for market in self._live_markets():
            # MAJORITY runs first, then fill polling, on the same market, in the
            # same pass. Its own trigger, its own fresh read, its own side and
            # its own submitter — the only thing it shares with the calls after
            # it is the market object, which it reads and never mutates.
            #
            # Driven under the operator gates, and for a deliberate reason: an
            # operator pressing Stop Trading must stop MAJORITY too, and the gate
            # that can still see that change is this one. A paused runtime
            # therefore does not evaluate the MAJORITY trigger at all, so a
            # window crossed while paused is not traded when the pause lifts —
            # the side would then be chosen from a book minutes newer than the
            # trigger.
            if (
                self._settings.majority.tradable
                and not self._paused
                and self._runtime.gate.submitting
            ):
                if health is None:
                    health = self.health()
                await self._majority.tick(market, health, now)
            report = await self._fills.poll(market.slug, now)
            self.stats.fills_recorded += len(report.new_fills)
            await self._reprice_open(market.slug, now)

    async def _reprice_open(self, market_slug: str, now: float) -> None:
        """Follow the book for MAJORITY's resting orders.

        Each order is routed to its window's repricer: a window-independent
        policy would move an order to a price inside a different band than the
        one the operator set for that window, and the gates that approved the
        order would no longer cover the resting price. The window-keyed repricer
        dict makes the per-window band lookup a single dict access.
        """
        for order in self._fills.unfilled(market_slug):
            if order.engine == MAJORITY_ENGINE:
                repricer = self._majority_repricers.get(order.offset_seconds)
                if repricer is None:
                    # The window this order was placed under is no longer
                    # configured (operator removed it). Leaving it untouched is
                    # the safe default: it is still a valid order against the
                    # window's frozen price, and cancelling it would close the
                    # only remaining MAJORITY position this market holds.
                    continue
                moved = await repricer.maybe_reprice(order, now)
                if moved.order_id != order.order_id:
                    self.stats.orders_repriced += 1

    def _live_markets(self) -> tuple[MarketInstance, ...]:
        return tuple(m for m in (self.rotator.current, self.rotator.closing) if m is not None)

    # ── settlement ───────────────────────────────────────────────────────────

    def _cleanup_market(self, slug: str) -> None:
        """Drop every per-market object the runtime holds for an archived market.

        Dropped on ARCHIVE rather than on CLOSE, alongside every other per-market
        object. A close-time drop would discard the state while the sweep is still
        retracting that market's orders, and the deck would show nothing for a
        market whose orders were still being cancelled. Thrown away, never reset
        (A11). Called for both archive paths: the rotation event and a settlement
        that archived directly (rotator.settled emits no event).
        """
        self._settlement.pop(slug, None)
        self.tokens.drop(slug)
        self._forget_book(slug)
        self._majority.drop_market(slug)

    async def _late_ptb_retry(self, now: float) -> None:
        """Fetch PTB for SETTLING markets that missed it during the active window.

        Polymarket publishes the previous market's finalPrice ~25s after close,
        which is often after the active-window PTB deadline has passed. Without
        this retry, a missing PTB leaves the market stuck in SETTLING forever
        because _settle_markets requires market.ptb to determine outcome.
        This method fetches the previous market's finalPrice one more time at
        settlement time, when it should be available.

        PTB is display-only for trading, but settlement outcome fundamentally
        requires a reference price (TWAP > PTB → UP, else DOWN). If PTB is
        still unavailable after this retry, settlement is postponed (not guessed).
        """
        if not isinstance(self._executor, PaperExecutor):
            return
        for market in self._live_markets():
            if market.phase is not MarketPhase.SETTLING:
                continue
            if now < market.close_ts + _SETTLE_GRACE_SECONDS:
                continue
            if market.ptb is not None:
                continue
            await self._cache_previous_close(market.window_ts)
            try:
                metadata = await self._discovery.fetch_metadata(market.slug)
            except FeedError:
                metadata = None
            if metadata is None:
                continue
            resolution = resolve_ptb(
                metadata,
                window_ts=market.window_ts,
                previous_close=self._previous_close.for_window(market.window_ts),
            )
            if not resolution.available:
                continue
            if freeze_ptb_for(market, resolution, logger=self._logger):
                assert resolution.value is not None
                self._store.save_ptb(market.slug, resolution.value, now)
                self.stats.ptb_frozen += 1
                log_event(
                    logging.INFO,
                    "PTB Resolved Late",
                    f"{market.slug} ptb={resolution.value} (fetched at settlement)",
                    logger=self._logger,
                )

        # Archived SETTLING markets are no longer in the rotator but still need
        # PTB and settlement. Same fetch logic, driven from storage rows rather
        # than live MarketInstance objects. After resolving PTB (or if it was
        # already saved), attempt settlement inline so these don't wait for a
        # restart.
        live_slugs = {m.slug for m in self._live_markets()}
        for slug in self._store.unsettled_markets():
            if slug in live_slugs:
                continue
            row = self._store.load_market_row(slug)
            if row is None:
                continue
            if str(row["phase"]) != MarketPhase.SETTLING.value:
                continue
            close_ts = int(row["close_ts"])
            window_ts = int(row["window_ts"])
            if now < close_ts + _SETTLE_GRACE_SECONDS:
                continue

            ptb: Decimal | None
            if row["ptb"] is not None:
                ptb = to_decimal(row["ptb"])
            else:
                await self._cache_previous_close(window_ts)
                try:
                    metadata = await self._discovery.fetch_metadata(slug)
                except FeedError:
                    metadata = None
                if metadata is None:
                    continue
                resolution = resolve_ptb(
                    metadata,
                    window_ts=window_ts,
                    previous_close=self._previous_close.for_window(window_ts),
                )
                if not resolution.available or resolution.value is None:
                    continue
                ptb = resolution.value
                self._store.save_ptb(slug, ptb, now)
                self.stats.ptb_frozen += 1
                log_event(
                    logging.INFO,
                    "PTB Resolved Late (archived)",
                    f"{slug} ptb={ptb} (fetched at settlement)",
                    logger=self._logger,
                )

            # Attempt settlement — same logic as _settle_recovered.
            twap: Decimal | None
            if row["settlement_twap"] is not None:
                twap = to_decimal(row["settlement_twap"])
            else:
                collector = SettlementTwapCollector(market_slug=slug, close_ts=close_ts)
                for obs in self._store.observations_between(
                    close_ts - SETTLEMENT_WINDOW_SECONDS, close_ts + 1
                ):
                    collector.offer(obs)
                twap = collector.settlement_twap
            if twap is None:
                continue
            self._store.save_settlement_twap(slug, twap, now)
            self._write_settlement(slug, twap, ptb, now)
            self._store.save_phase(slug, MarketPhase.SETTLED, now)
            log_event(
                logging.INFO,
                "Archived Market Settled",
                f"{slug}  twap {dec_str(twap)}  ptb {dec_str(ptb)}",
                logger=self._logger,
            )

    def _settle_markets(self, now: float) -> None:
        """Settle a market whose settlement window has been fully observed.

        V1 only: the paper venue's outcome IS the collected settlement TWAP,
        computed once the grace period past close has passed, compared against the
        PTB, and written as one settlement row per engine whose fills hold a
        position. V2's outcome arrives as a venue resolution event, which is
        Phase 3 work; until then a live market's row stays UNRESOLVED and recovery
        re-examines it at every restart.

        Level-triggered like everything else in the loop: every pass re-checks
        every SETTLING market, and a market whose collector is still incomplete
        simply waits for the next pass. Nothing is ever invented — a missing TWAP
        or a missing PTB postpones the settlement instead of guessing at one.
        """
        if not isinstance(self._executor, PaperExecutor):
            return
        for market in self._live_markets():
            if market.phase is not MarketPhase.SETTLING:
                continue
            if now < market.close_ts + _SETTLE_GRACE_SECONDS:
                continue
            collector = self._settlement.get(market.slug)
            twap = None if collector is None else collector.settlement_twap
            if twap is None or market.ptb is None:
                continue
            self._store.save_settlement_twap(market.slug, twap, now)
            self._write_settlement(market.slug, twap, market.ptb, now)
            archived = self.rotator.settled(market.slug, now)
            if archived:
                # settled() archives directly and emits no rotation event, so the
                # per-market cleanup an archived event would carry happens here.
                self._cleanup_market(archived)
            log_event(
                logging.INFO,
                "Market Settled",
                f"{market.slug}  twap {dec_str(twap)}  ptb {dec_str(market.ptb)}",
                logger=self._logger,
            )

    def _write_settlement(
        self, slug: str, twap: Decimal, ptb: Decimal, now: float
    ) -> None:
        """One settlement row per engine holding a position on this market.

        The outcome is the strict comparison, mirroring the engine's own direction
        rule: TWAP above PTB is UP, anything else DOWN. Equality resolves DOWN and
        a position on UP loses it — the same asymmetry the entry side already
        refuses to trade on. P&L is cost-valued: a winner pays out one per share,
        a loser nothing.
        """
        outcome = Outcome.UP if twap > ptb else Outcome.DOWN
        direction_by_order = {o.order_id: o.direction for o in self._store.orders_for(slug)}
        pnl_by_engine: dict[str, Decimal] = {}
        for fill in self._store.fills_for(slug):
            direction = direction_by_order.get(fill.order_id)
            if direction is None:
                continue
            won = direction.value == outcome.value
            delta = (
                fill.size * (Decimal(1) - fill.price) if won else -fill.size * fill.price
            )
            pnl_by_engine[fill.engine] = pnl_by_engine.get(fill.engine, _ZERO) + delta
        for engine in sorted(pnl_by_engine):
            self._store.save_settlement(
                Settlement(
                    market_slug=slug,
                    outcome=outcome,
                    settlement_twap=twap,
                    ptb=ptb,
                    settled_at=now,
                    pnl=pnl_by_engine[engine],
                    engine=engine,
                )
            )

    def _settle_recovered(self, now: float) -> None:
        """Settle markets a previous process closed but never wrote out.

        The same rule as the loop's settlement, run once over the SETTLING rows on
        disk at startup. The collector died with the process, so the TWAP is
        recomputed from the persisted observations across the venue's INCLUSIVE
        [close - 30s, close] window — the same samples, the same arithmetic, the
        same answer. A market without the observations or the PTB is left alone:
        UNRESOLVED is the honest row, and an invented settlement is not.
        """
        if not isinstance(self._executor, PaperExecutor):
            return
        for slug in self._store.unsettled_markets():
            row = self._store.load_market_row(slug)
            if row is None or row["ptb"] is None:
                continue
            if str(row["phase"]) != MarketPhase.SETTLING.value:
                # ACTIVE means the window reopened under the new process; a market
                # that is still trading is not settlement material.
                continue
            ptb = to_decimal(row["ptb"])
            twap: Decimal | None
            if row["settlement_twap"] is not None:
                twap = to_decimal(row["settlement_twap"])
            else:
                close_ts = int(row["close_ts"])
                collector = SettlementTwapCollector(market_slug=slug, close_ts=close_ts)
                for obs in self._store.observations_between(
                    close_ts - SETTLEMENT_WINDOW_SECONDS, close_ts + 1
                ):
                    collector.offer(obs)
                twap = collector.settlement_twap
            if twap is None:
                continue
            self._store.save_settlement_twap(slug, twap, now)
            self._write_settlement(slug, twap, ptb, now)
            self._store.save_phase(slug, MarketPhase.SETTLED, now)
            log_event(
                logging.INFO,
                "Recovered Market Settled",
                f"{slug}  twap {dec_str(twap)}  ptb {dec_str(ptb)}",
                logger=self._logger,
            )

    # ── venue metadata ───────────────────────────────────────────────────────

    async def _load_tokens(self, slug: str) -> None:
        """Cache the official token id for each side of one market.

        Labels are matched, never positions. `clobTokenIds` is an ordered list with
        no side information attached; taking index 0 as Up would place real orders
        on the opposite outcome on any market where the venue ordered them the other
        way, and nothing about the resulting order would look wrong.

        Read through the book client, which both modes have: V1 needs the same ids
        to read the same official book, and `get_market` is public data requiring
        no credential.
        """
        client = self._book_client or self._venue_client
        if client is None or self.tokens.known(slug):
            return
        try:
            market = await client.get_market(slug=slug)
        except Exception as exc:
            log_event(
                logging.WARNING,
                "Token Ids Unavailable",
                f"{slug}  {exc}",
                logger=self._logger,
            )
            return
        for outcome in (market.outcomes.yes, market.outcomes.no):
            if outcome.token_id is None:
                continue
            label = outcome.label.strip().lower()
            if label in _UP_LABELS:
                self.tokens.put(slug, Direction.UP, str(outcome.token_id))
            elif label in _DOWN_LABELS:
                self.tokens.put(slug, Direction.DOWN, str(outcome.token_id))

    # ── loops ────────────────────────────────────────────────────────────────

    async def _main_loop(self, market_target: int | None) -> None:
        """The level-triggered pass. Not a schedule (A12).

        `market_target=None` is the production case: run until cancelled.
        """
        while market_target is None or self.stats.markets_processed < market_target:
            now = self._clock.now()
            event = self.rotator.advance(now)
            if event.opened:
                self.stats.markets_processed += 1
                market = self.rotator.current
                if market is not None:
                    self._settlement[market.slug] = SettlementTwapCollector(
                        market_slug=market.slug, close_ts=market.close_ts
                    )
                    # Fresh MAJORITY state for the new market. Created here rather
                    # than lazily inside the engine's tick so that "this market has
                    # no MAJORITY state" stays a real fault the engine can refuse on,
                    # instead of a condition it silently repairs by inventing one
                    # mid-window (A11).
                    #
                    # restore_from_intents runs on every open — including on a
                    # market this process last saw in a previous lifetime. The
                    # intent for that lifetime is still on disk under the engine
                    # column; reconstruct_locked_side reads it back so the side
                    # lock survives a restart, and the matching order row keeps
                    # being the order the previous process placed (the UNIQUE
                    # constraint on (engine, market_slug, offset_seconds) prevents
                    # a second intent from being written). On a fresh market
                    # there are no intents and the call is a no-op.
                    self._majority.open_market(market.slug, market.close_ts)
                    self._majority.restore_from_intents(market.slug, now)
                    await self._load_tokens(market.slug)
                # Retry immediately on the new market rather than waiting out the
                # interval left over from the previous one.
                self._next_ptb_attempt = 0.0
                self._print_market_line(event.opened, event.closed)
            if event.closed:
                # Every remaining order on the closed market is retracted before its
                # settlement: an order still resting past close can fill against the
                # settled outcome, and that position was never approved by any gate.
                await self._sweeper.sweep(event.closed, now)
            if event.archived:
                self._cleanup_market(event.archived)

            await self._late_ptb_retry(now)
            self._settle_markets(now)
            await self._attempt_ptb(now)
            await self._refresh_books(now)
            await self._refresh_wallet(now)
            self._watchdog.evaluate()
            self._gate_on_health()
            await self._drive_execution(now)
            if self._notifier.summary_due(now):
                stats = self.stats
                await self._notifier.send_summary(
                    {
                        "markets_processed": stats.markets_processed,
                        "orders_submitted": stats.orders_submitted,
                        "fills_recorded": stats.fills_recorded,
                        "reconnects": stats.reconnects,
                        "trading_enabled": self._runtime.trading_enabled,
                        "execution_armed": self._runtime.execution_armed,
                    }
                )
            await asyncio.sleep(_TICK_SECONDS)

    def _gate_on_health(self) -> None:
        """A blocked feed disables trading. A recovered feed re-enables if spec is VERIFIED.

        The original design required spec.apply() to re-enable, but that only runs at
        shutdown — leaving a startup warmup or transient network hiccup as a permanent
        block for the rest of the session. Re-enabling here still requires VERIFIED spec
        status, preserving the security invariant while preventing latch-on-stale.
        """
        if self._watchdog.blocked and self._runtime.trading_enabled:
            self._runtime.disable_trading("FEED_STALE")
        elif not self._watchdog.blocked and not self._runtime.trading_enabled:  # noqa: SIM102
            # Feed recovered — re-enable if spec is VERIFIED (the authority check).
            if self._runtime.spec_status == SettlementSpecStatus.VERIFIED:
                disable_reason = self._runtime.reason
                if disable_reason in ("FEED_STALE", ""):
                    self._runtime.enable_trading()
                    log_event(
                        logging.INFO,
                        "Feed Recovered",
                        "trading re-enabled after feed staleness cleared",
                        logger=self._logger,
                    )

    async def _feed_loop(self) -> None:
        attempts = self._feed.connect_attempts
        async for frame in self._feed.messages():
            if self._feed.connect_attempts > attempts:
                self.stats.reconnects += self._feed.connect_attempts - attempts
                attempts = self._feed.connect_attempts
            self.stats.disconnects = self._feed.disconnects
            self._handle_frame(frame)

    async def recover(self, now: float) -> None:
        """Reconcile what the previous process left behind. Runs before any submission.

        Reconciliation before submission, always: an order this process does not
        know about may still be resting, and submitting on top of it doubles the
        position while both orders look entirely genuine (A14).
        """
        runner = RecoveryRunner(self._store, self._reconciler, self._fills, logger=self._logger)
        log_event(
            logging.INFO,
            "Recovery Started",
            f"{len(self._store.unsettled_markets())} unsettled markets to reconcile",
            logger=self._logger,
        )
        report = await runner.run(now)
        self.stats.recoveries += 1
        self._recovery = report
        # A previous process that died between close and archive left SETTLING rows
        # behind; settle them from the persisted observations now that every fill
        # for them has been reconciled.
        self._settle_recovered(now)
        if not report.safe_to_trade and self._runtime.trading_enabled:
            self._runtime.disable_trading("RECOVERY_UNRESOLVED")

    async def run(self, *, market_target: int | None = None) -> RuntimeStats:
        """Start every engine and run until cancelled.

        The feed task is cancelled by the main loop exiting rather than the other
        way round: the feed reconnects forever by design, so it can never be what
        ends the run.
        """
        self.status = RuntimeStatus.STARTING
        self.started_at = self._clock.now()
        # Counted on start rather than on clean exit: a process killed by OOM never
        # reaches an exit path, and those are precisely the restarts worth counting.
        self.restart_count += 1
        self._store.set_runtime_state(_RESTART_KEY, str(self.restart_count), self.started_at)
        # Bound here rather than at construction: the hub broadcasts to websocket
        # subscribers through call_soon_threadsafe, and the loop it must target is
        # the one actually running the runtime, not whichever loop built the object.
        self._hub.bind_loop(asyncio.get_running_loop())
        attach(self._hub, self._logger)
        self._print_header()
        # After attach, so the first line of the run is in the Signal Tank and in
        # Telegram rather than only on stdout.
        log_event(
            logging.INFO,
            "Runtime Started",
            f"mode {self.mode.value}  trading "
            f"{'ENABLED' if self._runtime.trading_enabled else 'DISABLED'}",
            logger=self._logger,
        )
        await self.recover(self.started_at)

        feed_task = asyncio.create_task(self._feed_loop())
        # The notifier is a subscriber like the websocket, not a step in any engine.
        # If it dies the runtime does not notice and must not: notifications are not
        # in the trading path.
        notify_task = asyncio.create_task(self._notifier.run(self._hub))
        self.status = RuntimeStatus.running_for(self.mode)
        # Distinct from "Runtime Started": that one is logged before recovery, and
        # recovery is the part that can take a while and can disable trading. READY
        # is the moment the loop is actually turning.
        log_event(
            logging.INFO,
            "Runtime Ready",
            f"{self.status}  feed {self._feed.url}",
            logger=self._logger,
        )
        self._print_verification()
        stop_reason = "normal"
        try:
            await self._main_loop(market_target)
        except asyncio.CancelledError:
            stop_reason = "shutdown"
            raise
        except Exception:
            stop_reason = "error"
            raise
        finally:
            self.status = RuntimeStatus.STOPPING
            log_event(
                logging.INFO,
                "Runtime Stopped",
                f"{self.stats.markets_processed} markets processed",
                logger=self._logger,
            )
            for task in (feed_task, notify_task):
                task.cancel()
            for task in (feed_task, notify_task):
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            self.status = RuntimeStatus.STOPPED
            ended_at = self._clock.now()
            self._save_session_row(stop_reason, ended_at)

        self._spec.apply(self._runtime)
        self._print_summary(self._clock.now() - self.started_at)
        return self.stats

    # ── end-of-session summary ───────────────────────────────────────────────

    def _save_session_row(self, stop_reason: str, ended_at: float) -> None:
        """Persist what THIS run did. Every field has an authoritative source.

        No field is derived from an assumption about how the system is supposed to
        behave. The three TWAP window counters are fixed at zero (the TWAP window
        engine has been removed) and the schema still requires the columns, so a
        future run must never claim window activity from a process that has no
        window engine.

        Order counts are read back from SQLite filtered on this session's start,
        because `stats.orders_submitted` counts submission calls while the fill rate
        must be a statement about rows that exist. Warnings and errors come from the
        event hub's monotonic counters.

        A field with no real source is written as NULL, never as a zero: zero is a
        measurement, and an unmeasured field claiming zero warnings reads as a clean
        run.
        """
        orders = self._store.order_tally_since(self.started_at)
        submitted = orders["submitted"]
        filled = orders["filled"]
        self._store.save_runtime_session(
            {
                "runtime_session_id": self.runtime_session_id,
                "mode": self.mode.value,
                "provider": self._settings.env.twap_provider,
                "git_commit": git_commit(),
                "start_reason": self.start_reason,
                "stop_reason": stop_reason,
                "started_at": self.started_at,
                "ended_at": ended_at,
                "duration_seconds": str(max(ended_at - self.started_at, 0.0)),
                "markets_seen": self.stats.markets_processed,
                # The TWAP Window Engine has been removed; these counters are fixed
                # at zero. The session schema requires the columns.
                "windows_frozen": 0,
                "windows_fired": 0,
                "windows_expired": 0,
                "orders_submitted": submitted,
                "orders_filled": filled,
                # NULL, not 0.0, when nothing was submitted: a run that placed no
                # orders has no fill rate, and "0%" would read as a run that placed
                # orders and filled none of them.
                "fill_rate": f"{filled / submitted:.4f}" if submitted else None,
                "reconnects": self.stats.reconnects,
                "disconnects": self.stats.disconnects,
                "recoveries": self.stats.recoveries,
                "warnings": self._hub.warning_count,
                "errors": self._hub.error_count,
                "final_status": self.status,
            }
        )

    # ── startup verification ─────────────────────────────────────────────────

    def verification(self) -> tuple[tuple[str, str, str], ...]:
        """The startup verification rows: (name, state, detail).

        Every row is a real reading taken at the moment it is called. Nothing here
        is assumed PASS because the process got this far — a row that cannot be
        checked reports what it is, not a pass.
        """
        provider = self._settings.env.twap_provider.upper()
        report = self._recovery
        rows: list[tuple[str, str, str]] = [
            ("Risk Gates", f"{len(GATE_ORDER)} / {len(GATE_ORDER)}", "G01-G19 registered"),
            (
                "Wallet",
                _verdict(self._wallet_status != _WALLET_DISCONNECTED),
                self._wallet_status or "not polled yet",
            ),
            ("Provider", _verdict(provider in _PROVIDERS), provider or "unset"),
            (
                "RTDS",
                _verdict(bool(self._feed.url)) if provider == "RTDS" else "NOT IN USE",
                self._feed.url if provider == "RTDS" else f"provider is {provider}",
            ),
            (
                "CLOB",
                _verdict(self._book_client is not None),
                "book client attached" if self._book_client is not None else "no book client",
            ),
            (
                "Database",
                _verdict(self._store.integrity_check() == "ok"),
                self._store.integrity_check(),
            ),
            (
                "Recovery",
                "PASS" if report is not None and report.safe_to_trade else "FAIL",
                "not run" if report is None else f"{len(report.orphans)} orphans",
            ),
            ("Supervisor", _verdict(self.supervisor_ready), self.supervisor_detail or "READY"),
        ]
        ready = all(state in ("PASS", "NOT IN USE") for _, state, _ in rows[1:])
        rows.append(("Ready", "YES" if ready else "NO", ""))
        return tuple(rows)

    def _print_verification(self) -> None:
        """Print the verification block exactly once, at the moment the loop starts."""
        lines = "".join(f"  {name:<12}{state}\n" for name, state, _ in self.verification())
        self._out.write(f"\nRuntime Verification\n{lines}\n")

    # ── output ───────────────────────────────────────────────────────────────

    def _print_header(self) -> None:
        gate = self._runtime.gate
        self._out.write(
            f"\narc run — mode {self.mode.value}\n"
            f"  feed        {self._feed.url}\n"
            f"  database    {self._store.path}\n"
            f"  trading     {'ENABLED' if gate.enabled else 'DISABLED'}  {gate.reason}\n"
            f"  execution   {'ARMED' if gate.armed else 'NOT ARMED'}\n\n"
        )

    def _print_market_line(self, opened: str, closed: str) -> None:
        """Report the rotation. The PTB is deliberately NOT printed here.

        At the instant a market opens its official opening reference has not been
        published yet, so printing it here would print UNAVAILABLE on every healthy
        market. The PTB gets its own line when it actually arrives.
        """
        ticks = self.stats.per_market_ticks.get(closed, 0) if closed else 0
        if closed:
            self._out.write(f"  rotated  {closed} closed after {ticks} ticks\n")
        self._out.write(f"  opened   {opened}\n")

    def _print_ptb_line(self, slug: str, resolution: PtbResolution) -> None:
        self._out.write(f"  ptb      {resolution.value}  {slug}  via {resolution.source}\n")

    def _print_summary(self, elapsed: float) -> None:
        stats = self.stats
        cadence = stats.observed_cadence_ms(elapsed)
        result = self._spec.result
        self._out.write(
            "\nruntime summary\n"
            f"  markets processed       {stats.markets_processed}\n"
            f"  ptb frozen              {stats.ptb_frozen}\n"
            f"  ptb unavailable         {stats.ptb_unavailable}\n"
            f"  observations accepted   {stats.observations_accepted}\n"
            f"  observations rejected   {stats.observations_rejected}\n"
            f"  feed cadence            "
            f"{'unknown' if cadence is None else f'{cadence:.0f} ms mean gap'}\n"
            f"  reconnects              {stats.reconnects}\n"
            f"  orders submitted        {stats.orders_submitted}\n"
            f"  orders repriced         {stats.orders_repriced}\n"
            f"  fills recorded          {stats.fills_recorded}\n"
            f"  settlement stream       "
            f"{'found' if stats.settlement_stream_found else 'NOT FOUND'}\n"
            f"  settlement samples      {stats.settlement_samples}\n"
            f"  spec status             {result.status.value}  {result.reason}\n"
            f"  unresolved              {', '.join(result.unresolved()) or 'none'}\n"
            f"  trading                 "
            f"{'ENABLED' if self._runtime.trading_enabled else 'DISABLED'}"
            f"  {self._runtime.reason}\n\n"
        )


def _messages_in(payload: object) -> tuple[Any, ...]:
    """Flatten the relay envelope. A list of ticks and a single tick both occur."""
    if isinstance(payload, list):
        return tuple(payload)
    if isinstance(payload, dict):
        inner = payload.get("payload", payload.get("data"))
        if isinstance(inner, list):
            return tuple(inner)
        if isinstance(inner, dict):
            return (inner,)
        return (payload,)
    return ()


def _environment(env: Any) -> Any:
    """The SDK's production environment, with only the configured URLs overridden.

    Applied to the SDK's own environment rather than to a hand-built one, so the
    thirty-odd contract addresses ARC never configures keep coming from the SDK and
    cannot drift.
    """
    if (env.clob_http_url, env.clob_ws_url) == (
        polymarket.PRODUCTION.clob_url,
        polymarket.PRODUCTION.clob_user_ws_url,
    ):
        return polymarket.PRODUCTION
    return replace(
        polymarket.PRODUCTION,
        clob_url=env.clob_http_url,
        clob_user_ws_url=env.clob_ws_url,
    )


async def build_executor(
    settings: Settings,
    store: Store,
    tokens: TokenCache,
    *,
    logger: logging.Logger | None = None,
) -> tuple[Executor, polymarket.AsyncSecureClient | None]:
    """The ONE component that differs between V1 and V2 (Q4).

    Returns the venue client alongside it because V2 needs the same authenticated
    client for official token metadata; V1 has none and returns None, which is what
    keeps the paper path free of any venue credential.
    """
    if settings.mode is not Mode.V2:
        return PaperExecutor(), None

    env = settings.env

    # The SDK signs and funds orders from ONE address (`wallet`; it becomes
    # `funder_address` on every order draft). Two different addresses cannot both be
    # honoured, so a config that names two is refused here rather than silently
    # dropping one — an order funded from the wrong account is rejected by the venue
    # for a reason that reads as a credential fault.
    proxy = env.polymarket_proxy_address.strip()
    funder = env.polymarket_funder.strip()
    if proxy and funder and proxy.lower() != funder.lower():
        raise ConfigInvariantError(
            f"POLYMARKET_PROXY_ADDRESS ({proxy}) and POLYMARKET_FUNDER ({funder}) "
            "differ. The official SDK signs and funds from a single address; set "
            "them to the same value or leave one blank."
        )
    wallet = proxy or funder or None

    # Endpoint overrides are applied to the SDK's own production environment rather
    # than to a hand-built one, so the thirty-odd contract addresses ARC never
    # configures keep coming from the SDK and cannot drift.
    environment = _environment(env)

    client = await polymarket.AsyncSecureClient.create(
        private_key=env.polymarket_private_key.get_secret_value(),
        wallet=wallet,
        environment=environment,
        credentials=polymarket.ApiKeyCreds(
            key=env.polymarket_api_key.get_secret_value(),
            secret=env.polymarket_api_secret.get_secret_value(),
            passphrase=env.polymarket_api_passphrase.get_secret_value(),
        ),
    )

    # An address the operator wrote down that does not match the one the key
    # actually controls is the single most expensive configuration error available:
    # every balance on the dashboard would belong to an account ARC cannot trade.
    expected = env.wallet_address.strip()
    actual = str(client.wallet)
    if expected and expected.lower() != actual.lower():
        raise ConfigInvariantError(
            f"WALLET_ADDRESS is {expected} but the configured private key controls "
            f"{actual}. The dashboard would report balances for an account ARC "
            "cannot trade from."
        )

    def local_id(market_slug: str, venue_order_id: str) -> str:
        """Adapts the store's positional lookup to the resolver's keyword protocol."""
        return store.local_order_id(market_slug, venue_order_id)

    return LiveExecutor(client, tokens, local_id, logger=logger), client


async def run_arc(
    settings: Settings,
    store: Store,
    clock: Clock,
    out: TextIO,
    *,
    market_target: int | None = None,
    logger: logging.Logger | None = None,
) -> int:
    """Startup steps 2-5 and the runtime loop. Returns a process exit code.

    THE DASHBOARD IS THE OUTER TASK. It is served first and outlives every
    runtime, because STOP shuts the runtime down and the operator still has to be
    able to see that it worked and start the other mode. The supervisor owns the
    runtime underneath it and rebuilds it on every start.

    `--mode` auto-starts that runtime here rather than leaving the process idle:
    a restart under PM2 must come back to a running system without a human
    pressing START. It comes back DISARMED, so coming back running is not coming
    back trading.

    Returns 0 even when the spec could not be verified. A non-zero exit would tell
    PM2 to restart, and restarting changes nothing about an unverifiable spec while
    losing the in-memory market and the observations it had collected.
    """
    from arc.runtime.supervisor import RuntimeSupervisor

    # Bind is checked before anything starts: a non-loopback bind must fail the
    # startup, not warn during it.
    host = check_bind(settings.env.api_bind)
    supervisor = RuntimeSupervisor(
        settings=settings, store=store, clock=clock, out=out, logger=logger
    )
    dashboard = asyncio.create_task(
        serve_dashboard(
            supervisor.runtime, host=host, port=settings.env.api_port, supervisor=supervisor
        )
    )
    try:
        await supervisor.start(settings.mode, market_target=market_target)
        await supervisor.wait()
        if market_target is None:
            # Production: the loop only ends when the operator stops it, so hold the
            # process open on the dashboard. A bounded run has finished its markets
            # and returns.
            await dashboard
    finally:
        await supervisor.aclose()
        dashboard.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await dashboard
    return 0
