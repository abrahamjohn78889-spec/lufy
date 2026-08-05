"""`arc observe`: run the market engine with no trading, ever.

Startup order is the one specified in A8, and the order matters:

    1  load config and validate the fatal invariants     (a bad config DOES refuse)
    2  open SQLite, migrate, reconcile
    3  start the API and dashboard                       (not in this phase's scope)
    4  connect the feeds
    5  automatic settlement-spec verification

THE PROCESS ALWAYS STARTS past step 1. A feed that will not connect, a settlement
stream that turns out to be the wrong one, an unverifiable spec — none of these exit.
They set `trading_enabled = False` with a recorded reason and everything else keeps
running: feeds retrying, TWAP accumulating, markets rotating, observations persisted.

This command cannot trade. Not because a flag is set, but because nothing in this
module and nothing it imports can submit an order — there is no execution adapter in
the import graph at all. That is a stronger guarantee than a disabled flag, which is
why the observation runtime is a separate entry point rather than `arc run --dry`.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Final, TextIO

from arc.clock import Clock
from arc.config import Settings
from arc.domain.enums import MarketPhase
from arc.domain.models import Observation
from arc.errors import FeedError, ObservationRejectedError
from arc.logging_setup import log_event
from arc.market.discovery import MarketDiscovery, open_discovery
from arc.market.feed import RtdsFeed
from arc.market.ptb import BoundaryReference, freeze_ptb_for, resolve_ptb
from arc.market.rotation import MarketRotator
from arc.market.settlement_feed import SettlementTwapCollector
from arc.market.spec_check import SpecChecker
from arc.market.validation import ObservationValidator
from arc.market.watchdog import FeedWatchdog
from arc.runtime.state import RuntimeState
from arc.storage.store import Store

__all__ = ["ObservationRun", "ObserveStats", "observe"]

# The symbol the Chainlink stream reports for the pair these markets settle on.
EXPECTED_SYMBOL: Final[str] = "BTC/USD"

# How often the level-triggered rotation check runs. Fine enough that a boundary is
# noticed within a fraction of a second, coarse enough to cost nothing. Not a
# schedule: the check compares the clock against the boundary every time (A12).
_TICK_SECONDS: Final[float] = 0.2


@dataclass(slots=True)
class ObserveStats:
    """What the run saw. Printed at the end; feeds no decision."""

    markets_observed: int = 0
    ptb_frozen: int = 0
    ptb_unavailable: int = 0
    observations_accepted: int = 0
    observations_rejected: int = 0
    settlement_samples: int = 0
    settlement_stream_found: bool = False
    reconnects: int = 0
    per_market_ticks: dict[str, int] = field(default_factory=dict)

    def observed_cadence_ms(self, elapsed_seconds: float) -> float | None:
        """Mean gap between accepted observations, for the report only.

        TRAP 1: this number says nothing about the settlement TWAP window length and
        is never used to infer or check it. It is reported because the operator asked
        what cadence the feed actually runs at.
        """
        if self.observations_accepted < 2 or elapsed_seconds <= 0:
            return None
        return elapsed_seconds * 1000.0 / self.observations_accepted


class ObservationRun:
    """One observation session. Owns the rotator, the feed and the validators.

    Everything mutable is an attribute of this object. A second run in the same
    process would be a second instance with its own markets, its own accumulators and
    its own validator history (A11).
    """

    __slots__ = (
        "_boundary",
        "_clock",
        "_discovery",
        "_feed",
        "_logger",
        "_out",
        "_runtime",
        "_settings",
        "_settlement",
        "_spec",
        "_store",
        "_validator",
        "_watchdog",
        "rotator",
        "stats",
    )

    def __init__(
        self,
        *,
        settings: Settings,
        store: Store,
        clock: Clock,
        runtime: RuntimeState,
        discovery: MarketDiscovery,
        feed: RtdsFeed,
        out: TextIO,
        logger: logging.Logger | None = None,
    ) -> None:
        trading = settings.trading
        self._settings = settings
        self._store = store
        self._clock = clock
        self._runtime = runtime
        self._discovery = discovery
        self._feed = feed
        self._out = out
        self._logger = logger
        self._validator = ObservationValidator()
        self._watchdog = FeedWatchdog(
            clock,
            warn_ms=trading.feed_stale_warn_ms,
            critical_ms=trading.feed_stale_critical_ms,
        )
        self._spec = SpecChecker(logger=logger)
        self._settlement: dict[str, SettlementTwapCollector] = {}
        # The boundary reference gates the L2 PTB source. None until a boundary has
        # been observed on an unbroken connection (see ptb.py).
        self._boundary: BoundaryReference | None = None
        self.rotator = MarketRotator(
            store,
            clock,
            offsets=trading.windows_by_priority,
            logger=logger,
        )
        self.stats = ObserveStats()

    # ── PTB ──────────────────────────────────────────────────────────────────

    async def _freeze_ptb(self, slug: str) -> None:
        """Resolve and freeze the official PTB for the market that just opened.

        A metadata failure is not fatal to the run: the market is marked DEAD, the
        line is logged, and the process continues with the next market (A1 Rule 1).
        """
        market = self.rotator.current
        if market is None or market.slug != slug or market.ptb is not None:
            return
        try:
            discovered = await self._discovery.fetch_metadata(slug)
        except FeedError as exc:
            market.phase = MarketPhase.DEAD
            market.dead_reason = "PTB_UNAVAILABLE"
            self._store.save_phase(slug, MarketPhase.DEAD, self._clock.now(), "PTB_UNAVAILABLE")
            log_event(
                logging.ERROR,
                "PTB Unavailable",
                f"{slug} — no trading this market ({exc})",
                logger=self._logger,
            )
            self.stats.ptb_unavailable += 1
            return

        resolution = resolve_ptb(
            discovered, window_ts=market.window_ts, boundary=self._boundary
        )
        if freeze_ptb_for(market, resolution, logger=self._logger):
            assert resolution.value is not None
            self._store.save_ptb(slug, resolution.value, self._clock.now())
            self.stats.ptb_frozen += 1
        else:
            self._store.save_phase(
                slug, MarketPhase.DEAD, self._clock.now(), market.dead_reason
            )
            self.stats.ptb_unavailable += 1

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
        self._record_boundary(observation)

        for market in self.rotator.route(observation):
            self._store.save_observation(market.slug, observation, received_at)
            self.stats.per_market_ticks[market.slug] = (
                self.stats.per_market_ticks.get(market.slug, 0) + 1
            )
            collector = self._settlement.get(market.slug)
            if collector is not None and collector.offer(observation):
                self.stats.settlement_samples += 1
                self.stats.settlement_stream_found = True

    def _record_boundary(self, observation: Observation) -> None:
        """Keep the observation that lands on a 300s boundary, with its continuity.

        The continuity flag comes from the feed's own BoundaryTracker, not from
        anything inferred here: only the connection knows whether it stayed up across
        the boundary, and that is what makes the recorded price official rather than
        an estimate (ptb.py L2).
        """
        boundary_ts = int(observation.ts) - int(observation.ts) % 300
        if abs(observation.ts - boundary_ts) > 1.0:
            return
        continuous = self._feed.boundary.observe_boundary()
        self._boundary = BoundaryReference(
            boundary_ts=boundary_ts,
            price=observation.price,
            observed_ts=observation.ts,
            continuous=continuous,
        )

    # ── loops ────────────────────────────────────────────────────────────────

    async def _rotation_loop(self, market_target: int) -> None:
        """Level-triggered rotation. Not a schedule (A12)."""
        while self.stats.markets_observed < market_target:
            now = self._clock.now()
            event = self.rotator.advance(now)
            if event.opened:
                self.stats.markets_observed += 1
                market = self.rotator.current
                if market is not None:
                    self._settlement[market.slug] = SettlementTwapCollector(
                        market_slug=market.slug, close_ts=market.close_ts
                    )
                await self._freeze_ptb(event.opened)
                self._print_market_line(event.opened, event.closed)
            self._watchdog.evaluate()
            self._gate_on_health()
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
            self._handle_frame(frame)

    async def run(self, *, market_target: int = 3) -> ObserveStats:
        """Observe `market_target` consecutive markets, then stop. Never trades.

        The feed task is cancelled when the rotation target is reached rather than
        the other way round: the feed loop reconnects forever by design, so the
        market count is what bounds the run.
        """
        started = self._clock.now()
        self._print_header()

        feed_task = asyncio.create_task(self._feed_loop())
        try:
            await self._rotation_loop(market_target)
        finally:
            feed_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await feed_task

        self._spec.apply(self._runtime)
        self._print_summary(self._clock.now() - started)
        return self.stats

    # ── output ───────────────────────────────────────────────────────────────

    def _print_header(self) -> None:
        gate = self._runtime.gate
        self._out.write(
            "\narc observe — market engine, no trading\n"
            f"  feed        {self._feed.url}\n"
            f"  database    {self._store.path}\n"
            f"  trading     {'ENABLED' if gate.enabled else 'DISABLED'}"
            f"  {gate.reason}\n\n"
        )

    def _print_market_line(self, opened: str, closed: str) -> None:
        market = self.rotator.current
        ptb = market.ptb if market is not None else None
        ticks = self.stats.per_market_ticks.get(closed, 0) if closed else 0
        detail = f"  ptb {ptb}" if ptb is not None else "  ptb UNAVAILABLE — market DEAD"
        if closed:
            self._out.write(f"  rotated  {closed} closed after {ticks} ticks\n")
        self._out.write(f"  opened   {opened}{detail}\n")

    def _print_summary(self, elapsed: float) -> None:
        stats = self.stats
        cadence = stats.observed_cadence_ms(elapsed)
        result = self._spec.result
        self._out.write(
            "\nobservation summary\n"
            f"  markets observed        {stats.markets_observed}\n"
            f"  ptb frozen              {stats.ptb_frozen}\n"
            f"  ptb unavailable         {stats.ptb_unavailable}\n"
            f"  observations accepted   {stats.observations_accepted}\n"
            f"  observations rejected   {stats.observations_rejected}\n"
            f"  feed cadence            "
            f"{'unknown' if cadence is None else f'{cadence:.0f} ms mean gap'}\n"
            f"  reconnects              {stats.reconnects}\n"
            f"  settlement stream       "
            f"{'found' if stats.settlement_stream_found else 'NOT FOUND'}\n"
            f"  settlement samples      {stats.settlement_samples}\n"
            f"  spec status             {result.status.value}  {result.reason}\n"
            f"  unresolved              "
            f"{', '.join(result.unresolved()) or 'none'}\n"
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


async def observe(
    settings: Settings,
    store: Store,
    clock: Clock,
    out: TextIO,
    *,
    market_target: int = 3,
    logger: logging.Logger | None = None,
) -> int:
    """Startup steps 2-5 and the observation loop. Returns a process exit code.

    Returns 0 even when the spec could not be verified. A non-zero exit would tell
    PM2 to restart, and restarting changes nothing about an unverifiable spec while
    losing the in-memory market and the observations it had collected.
    """
    async with open_discovery(logger=logger) as discovery:
        runtime = RuntimeState(store, clock)
        runtime.load()
        run = ObservationRun(
            settings=settings,
            store=store,
            clock=clock,
            runtime=runtime,
            discovery=discovery,
            feed=RtdsFeed(clock, logger=logger),
            out=out,
            logger=logger,
        )
        await run.run(market_target=market_target)
    return 0
