"""The unified runtime. TWO modes, and nothing else: V1 paper, V2 live.

    V1   the COMPLETE pipeline with PaperExecutor
    V2   the COMPLETE pipeline with LiveExecutor

There is no third mode. V1 is not a lightweight simulator and not an observation
run: market engine, window engine, decision engine, risk engine, limit order
engine, fills, reprice, sweep, reconcile and recovery all execute identically.
The ONLY component that differs between the two is the executor, which is why
this file selects it once at construction and nothing below that line branches
on mode. A second runtime path is a second set of behaviour to keep in sync, and
the paper evidence would stop being evidence about the live run.

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
from collections import deque
from collections.abc import Awaitable
from dataclasses import dataclass, field, replace
from decimal import Decimal
from time import perf_counter
from typing import Any, Final, Protocol, TextIO

import polymarket

from arc.api.app import check_bind
from arc.api.app import serve as serve_dashboard
from arc.clock import Clock, DriftMonitor, DriftStatus
from arc.config import Settings
from arc.decision.engine import DecisionEngine, RuntimeHealth
from arc.decision.quota import QuotaLedger
from arc.domain.enums import Direction, MarketPhase, Mode
from arc.domain.models import MarketInstance, Order
from arc.domain.money import dec_str
from arc.domain.timing import MARKET_DURATION_SECONDS, slug_for
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
from arc.logging_setup import log_event
from arc.market.discovery import MarketDiscovery
from arc.market.providers import TwapProvider
from arc.market.ptb import (
    DEAD_REASON_PTB_UNAVAILABLE,
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
from arc.strategy.config import config_from_trading
from arc.strategy.registry import default_registry
from arc.windows.engine import WindowEngine

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
        "_decisions",
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
        "_next_book_read",
        "_next_ptb_attempt",
        "_next_wallet_read",
        "_notifier",
        "_out",
        "_paused",
        "_previous_close",
        "_reconciler",
        "_recovery",
        "_repricer",
        "_risk",
        "_runtime",
        "_settings",
        "_settlement",
        "_spec",
        "_store",
        "_submitter",
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
        "started_at",
        "stats",
        "status",
        "supervisor_detail",
        "supervisor_ready",
        "supervisor_state",
        "tokens",
        "windows",
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
    ) -> None:
        trading = settings.trading
        self._settings = settings
        self._store = store
        self._clock = clock
        self._runtime = runtime
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
        self._logger = logger
        # Pause is the operator's "hold new submissions" and is deliberately
        # separate from disarming: pausing must not clear the arm state, or resuming
        # would silently require a second confirmation the operator did not expect.
        self._paused = False
        self._hub = EventHub()
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
        self._bucket = TokenBucket(
            sustained=trading.outbound_rate_sustained,
            burst=trading.outbound_rate_burst,
            now=clock.now(),
        )
        self._submitter = Submitter(
            store,
            executor,
            bucket=self._bucket,
            minimum=trading.min_tradable_size,
            logger=logger,
        )
        self._fills = FillEngine(store, executor, logger=logger)
        self._repricer = Repricer(
            store,
            executor,
            RepricePolicy(
                band_min=trading.entry_price_min,
                band_max=trading.entry_price_max,
                tick=trading.tick_size,
            ),
            bucket=self._bucket,
            logger=logger,
        )
        self._sweeper = Sweeper(store, executor, logger=logger)
        self._reconciler = Reconciler(store, executor, logger=logger)

        # ── decision half ────────────────────────────────────────────────────
        # Owned here so the Systems page can read the timings without reaching
        # through the Decision Engine into a gate it is not allowed to time.
        self._risk = TimedRiskEngine()
        self._decisions = DecisionEngine(
            store,
            strategy_config=config_from_trading(trading),
            limits=limits_from_trading(trading),
            registry=default_registry(),
            quota=QuotaLedger(
                max_trades_per_market=trading.max_trades_per_market,
                min_tradable_size=trading.min_tradable_size,
            ),
            quote_source=self._quote,
            health_source=self.health,
            risk=self._risk,
            logger=logger,
        )

        # The Window Engine holds no per-market state, so one instance correctly
        # serves both markets alive across a close boundary (A11).
        self.windows = WindowEngine(store, trading, logger=logger)
        self.rotator = MarketRotator(
            store,
            clock,
            offsets=trading.windows_by_priority,
            windows=self.windows,
            decisions=self._decisions,
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
        report = self._recovery
        orphans = () if report is None else report.orphans
        standing: dict[str, tuple[bool, str]] = {
            "trading_enabled": (gate.enabled, gate.reason or "enabled"),
            "execution_armed": (gate.armed, "armed" if gate.armed else "not armed"),
            "strategy_enabled": (
                self._decisions.strategy_count > 0,
                f"{self._decisions.strategy_count} registered",
            ),
            "loss_limits": (
                health.daily_loss_usd <= self._decisions.limits.max_daily_loss_usd
                and health.consecutive_losses < self._decisions.limits.max_consecutive_losses,
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
    def decisions(self) -> DecisionEngine:
        """The Decision Engine. Read-only from here; the loop is what drives it."""
        return self._decisions

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

    def _quote(self, market_slug: str, direction: Direction) -> Decimal | None:
        """The book price the strategy sizes against.

        Synchronous because the decision pass is synchronous, and answered from the
        cache `_refresh_books` fills from the official CLOB. A quote older than
        `_BOOK_MAX_AGE_SECONDS` is reported as absent rather than returned: the
        window then skips with NO_QUOTE, which is the correct outcome for a book
        nobody could read, whereas a stale price would be sized against as if it
        were current.
        """
        cached = self._book.get((market_slug, direction))
        if cached is None:
            return None
        price, read_at = cached
        if self._clock.now() - read_at > _BOOK_MAX_AGE_SECONDS:
            return None
        return price

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
        """Mark the market DEAD only once its earliest execution window has passed.

        Before that instant an unresolved PTB is a value that has not been published
        yet and the correct response is to try again. After it, no PTB can arrive in
        time, and leaving the market PENDING forever would hide a permanently
        unusable market behind a hopeful state.
        """
        if now < self._ptb_deadline(market):
            return
        market.phase = MarketPhase.DEAD
        market.dead_reason = DEAD_REASON_PTB_UNAVAILABLE
        self._store.save_phase(market.slug, MarketPhase.DEAD, now, DEAD_REASON_PTB_UNAVAILABLE)
        log_event(
            logging.ERROR,
            "PTB Unavailable",
            f"{market.slug} — no trading this market ({detail})",
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
        except ObservationRejectedError:
            self.stats.observations_rejected += 1
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

        # Re-evaluate frozen triggers now, not on the next tick. The signal TWAP only
        # moves when an observation lands, so this is the only instant at which a
        # trigger can newly become satisfied; deferring it to the 200 ms loop would
        # make the check sampled rather than continuous (A12).
        self.rotator.evaluate_windows(received_at)

    # ── the limit order engine ───────────────────────────────────────────────

    async def _drive_execution(self, now: float) -> None:
        """Submit, fill, reprice. Level-triggered and idempotent, like everything else.

        Reads its work from SQLite rather than from a queue: an intent persisted but
        not yet submitted is discovered on the next pass regardless of what killed
        the process in between, which is what makes a restart resume instead of drop
        the window.
        """
        for market in self._live_markets():
            await self._submit_pending(market, now)
            report = await self._fills.poll(market.slug, now)
            self.stats.fills_recorded += len(report.new_fills)
            await self._reprice_open(market.slug, now)

    async def _submit_pending(self, market: MarketInstance, now: float) -> None:
        """Submit every persisted intent that has no order yet.

        BOTH gates are re-checked here and not only inside the risk engine. The
        gates were evaluated when the intent was created; an operator pressing Stop
        Trading, or ARC disabling trading, in the milliseconds between creation and
        submission must stop the order, and the only place that can still see that
        change is this one. Pause is checked in the same breath and for the same
        reason.
        """
        if market.phase is not MarketPhase.ACTIVE:
            return
        if self._paused or not self._runtime.gate.submitting:
            return
        submitted_offsets = {o.offset_seconds for o in self._store.orders_for(market.slug)}
        for intent in self._store.intents_for(market.slug):
            if intent.offset_seconds in submitted_offsets:
                continue
            orders = await self._submitter.submit(
                intent,
                count=self._settings.trading.submission_count,
                phase=market.phase,
                now=now,
            )
            self.stats.orders_submitted += len(orders)

    async def _reprice_open(self, market_slug: str, now: float) -> None:
        for order in self._fills.unfilled(market_slug):
            moved: Order = await self._repricer.maybe_reprice(order, now)
            if moved.order_id != order.order_id:
                self.stats.orders_repriced += 1

    def _live_markets(self) -> tuple[MarketInstance, ...]:
        return tuple(m for m in (self.rotator.current, self.rotator.closing) if m is not None)

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
                self._settlement.pop(event.archived, None)
                self.tokens.drop(event.archived)
                self._forget_book(event.archived)

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
        """A blocked feed disables trading; a recovered feed does not re-enable it.

        Re-enabling is the spec check's job and requires VERIFIED status. A watchdog
        that could enable trading would be a second, weaker authority over the same
        flag, and the weaker one would win whenever data happened to be flowing.
        """
        if self._watchdog.blocked and self._runtime.trading_enabled:
            self._runtime.disable_trading("FEED_STALE")

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
        try:
            await self._main_loop(market_target)
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

        self._spec.apply(self._runtime)
        self._print_summary(self._clock.now() - self.started_at)
        return self.stats

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
