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
from arc.domain.models import MarketInstance
from arc.domain.timing import MARKET_DURATION_SECONDS, slug_for
from arc.errors import FeedError, ObservationRejectedError
from arc.logging_setup import log_event
from arc.market.discovery import MarketDiscovery, open_discovery
from arc.market.feed import RtdsFeed
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
from arc.runtime.state import RuntimeState
from arc.storage.store import Store

__all__ = ["ObservationRun", "ObserveStats", "observe"]

# The symbol the Chainlink stream reports for the pair these markets settle on.
EXPECTED_SYMBOL: Final[str] = "BTC/USD"

# How often the level-triggered rotation check runs. Fine enough that a boundary is
# noticed within a fraction of a second, coarse enough to cost nothing. Not a
# schedule: the check compares the clock against the boundary every time (A12).
_TICK_SECONDS: Final[float] = 0.2

# How often an unresolved PTB is retried. The venue publishes a market's
# `eventMetadata` roughly 25 seconds after it closes — which is roughly 25 seconds
# into the NEXT market's life — so a market that opens without a PTB is not yet a
# dead market, it is a market whose official opening reference has not been written
# yet. Polling every 5 seconds finds it within one interval of publication and costs
# a handful of requests per market.
_PTB_RETRY_SECONDS: Final[float] = 5.0


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
        "_clock",
        "_discovery",
        "_feed",
        "_logger",
        "_next_ptb_attempt",
        "_out",
        "_previous_close",
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
        # The L2 PTB source: the venue's published finalPrice for the previous market,
        # cached the moment it appears (see ptb.py). Instance state, never module
        # state — two runs in one process must not share a cached price (A11).
        self._previous_close = PreviousClosePtbCache()
        self._next_ptb_attempt: float = 0.0
        self.rotator = MarketRotator(
            store,
            clock,
            offsets=trading.windows_by_priority,
            logger=logger,
        )
        self.stats = ObserveStats()

    # ── PTB ──────────────────────────────────────────────────────────────────

    async def _attempt_ptb(self, now: float) -> None:
        """Try to obtain the official PTB for the live market. Retries until too late.

        A market that opens without a PTB is NOT yet a dead market. The venue writes a
        market's `eventMetadata` roughly 25 seconds after it closes, so the live
        market's official opening reference — which is the previous market's published
        `finalPrice` — does not exist at the instant the market opens and appears a few
        seconds later. Marking the market DEAD on the first miss would kill every
        single market for a value that was about to be published.

        The retry is bounded by the market's own earliest execution window. Past that
        instant a PTB would arrive too late to be of any use, so the market is marked
        DEAD and the reason recorded, exactly as A1 Rule 1 requires. Nothing is
        estimated at any point; the market simply is not traded.
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
        that finds nothing is the ordinary case and not worth a log line; the caller
        discovers the absence by the PTB staying unresolved.
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
        yet, and the correct response is to try again. After it, no PTB can arrive in
        time to be used, and leaving the market PENDING forever would hide a
        permanently unusable market behind a hopeful state.
        """
        if now < self._ptb_deadline(market):
            return
        market.phase = MarketPhase.DEAD
        market.dead_reason = DEAD_REASON_PTB_UNAVAILABLE
        self._store.save_phase(
            market.slug, MarketPhase.DEAD, now, DEAD_REASON_PTB_UNAVAILABLE
        )
        log_event(
            logging.ERROR,
            "PTB Unavailable",
            f"{market.slug} — no trading this market ({detail})",
            logger=self._logger,
        )
        self.stats.ptb_unavailable += 1

    def _ptb_deadline(self, market: MarketInstance) -> float:
        """The last instant a PTB could still be used: the first window's activation.

        Derived from the configured offsets rather than a constant, so a change to the
        window set moves the deadline with it instead of silently disagreeing.
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

        for market in self.rotator.route(observation):
            self._store.save_observation(market.slug, observation, received_at)
            self.stats.per_market_ticks[market.slug] = (
                self.stats.per_market_ticks.get(market.slug, 0) + 1
            )
            collector = self._settlement.get(market.slug)
            if collector is not None and collector.offer(observation):
                self.stats.settlement_samples += 1
                self.stats.settlement_stream_found = True

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
                # Retry immediately on the new market rather than waiting out the
                # interval left over from the previous one.
                self._next_ptb_attempt = 0.0
                self._print_market_line(event.opened, event.closed)
            await self._attempt_ptb(now)
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
        """Report the rotation. The PTB is deliberately NOT printed here.

        At the instant a market opens its official opening reference has not been
        published yet — the venue writes it a few seconds later — so printing it here
        would print UNAVAILABLE on every healthy market. The PTB gets its own line
        when it actually arrives.
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
