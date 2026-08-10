"""MAJORITY per-market engine: trigger, fresh read, side selection, intent, submission.

One instance per process. Keyed internally by (market_slug, window_seconds) so a
market with N configured windows holds N independent state objects. Per-market state
objects are created when a market opens and DROPPED when it closes — the same A11
discipline MarketInstance follows. There is no reset(), clear() or reuse path.

THE TWO-STEP RULE
=================
Step 1 (trigger): the book is polled each tick. When
    max(best_bid(UP), best_bid(DOWN)) >= config.trigger_price
the trigger fires. Exactly once per market/window.

Step 2 (determination): AFTER the trigger, a FRESH `executor.best_price` call is
made for each side. These reads are independent of the interval-cached `_book` dict
in ArcRuntime — using that cache would compare the same cached numbers twice, and
"two-step" would be two lookups of one read. The side with the higher fresh bid
wins. Equal bids → INDETERMINATE → NO_TRADE.

The side that crossed the trigger is NOT necessarily the side bought. A book that
read UP 0.91/DOWN 0.85 to satisfy a 0.90 trigger could read UP 0.16/DOWN 0.85 on
the fresh pass and yield DOWN. That is correct.

MULTI-WINDOW
============
A single tick advances EVERY configured window for a market. Each window has its
own state object, its own side lock, its own intent and its own order. A 3s and a
90s window run side by side; neither sees the other's book or side. The per-window
trigger evaluation is identical to the single-window case, parameterised by the
window's own configuration.

QUOTA ISOLATION
===============
MAJORITY keeps its own submitted/filled accounting and never touches
`market.reservations` (the shared set TWAP uses). Sharing it would let a MAJORITY
reservation consume TWAP's quota slot for the same offset, and vice versa. The
MAJORITY quota is simpler than TWAP's: at most one trade per market/window ever,
so `_submitted` is a set of (slug, window) pairs that have already reached the
submission path.

PERSISTENT SIDE LOCK
====================
A side lock decided by `select_side` lives only in memory. A restart loses it.
Reconstruction reads the persisted ExecutionIntent for each (market, window) and
materialises the locked-side state via `reconstruct_locked_side`, so a restarted
process resumes a locked-side market without re-reading the book and without
risking a second determination that disagrees with the persisted one.

GATE 5 (price_to_beat)
======================
RiskContext requires a positive PTB. MAJORITY has no conceptual PTB dependency —
it never compares against it — but the gate runs for every engine. `market.ptb` is
passed through unchanged: it is either already frozen (normal case) or None
(pre-freeze edge), in which case G05 denies and the window waits for the next tick.
"""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Final

from arc.decision.engine import RuntimeHealth
from arc.domain.enums import Direction
from arc.domain.models import ExecutionIntent, MarketInstance
from arc.errors import MarketPhaseError
from arc.execution.protocol import Executor
from arc.execution.submit import Submitter
from arc.logging_setup import log_event
from arc.majority.config import MAJORITY_ENGINE, MajorityConfig, MajorityWindowConfig
from arc.majority.identity import majority_intent_id_for, majority_trace_id_for
from arc.majority.state import (
    MajorityMarketState,
    MajorityState,
    MajorityStateError,
)
from arc.majority.trigger import (
    BookSnapshot,
    determine_majority,
    is_triggered,
)
from arc.risk.engine import RiskContext, RiskEngine, RiskVerdict
from arc.risk.limits import RiskLimits
from arc.storage.store import Store

__all__ = ["MajorityEngine"]

_ZERO: Final[Decimal] = Decimal("0")
_FRESH_MAX_AGE_SECONDS: Final[float] = 2.0  # a read older than this is stale


def _limits_from_window(window: MajorityWindowConfig) -> RiskLimits:
    """Project one MAJORITY window into the risk layer's view.

    MAJORITY has no per-market trade quota (at most one trade per market/window is
    enforced by the side-lock and the `_submitted` set, not by a quota ledger).
    The values that have no MAJORITY equivalent default to the most permissive
    safe value so the gate passes: max_trades and max_concurrent are set high;
    loss limits are disabled (zero); allow_opposing is False (MAJORITY always
    buys one side per market, never both).
    """
    return RiskLimits(
        max_trades_per_market=1,          # one MAJORITY trade per market/window, ever
        max_concurrent_positions=999,     # process-level; runtime passes the real count
        max_daily_loss_usd=_ZERO,         # zero disables the gate (see gate 13)
        max_consecutive_losses=0,         # zero disables the gate
        entry_price_min=window.entry_price_min,
        entry_price_max=window.entry_price_max,
        min_tradable_size=window.shares,  # the only size this window ever submits
        allow_opposing_directions=False,
    )


def _window_key(slug: str, window_seconds: int) -> tuple[str, int]:
    """The internal state dict key. Tuple for hashing and for unpacking.

    Tuple keying means two markets with the same window live under different keys
    (the slug differs), and one market with two windows lives under two different
    keys (the window differs). A bug that re-uses one market's state for another
    market's window is therefore impossible to express at the dict level.
    """
    return (slug, window_seconds)


class MajorityEngine:
    """Drives the MAJORITY sequence for every live market and every configured window.

    Stateless except for two collections keyed by `(slug, window_seconds)`:
      _states    — one MajorityMarketState per open market/window
      _submitted — `(slug, window)` pairs that have already reached the submission step

    Both are populated on open_market() and dropped on drop_market(). No
    (slug, window) persists past its market close boundary.
    """

    __slots__ = (
        "_config",
        "_executor",
        "_logger",
        "_risk",
        "_states",
        "_store",
        "_submitted",
        "_submitter",
    )

    def __init__(
        self,
        config: MajorityConfig,
        store: Store,
        executor: Executor,
        submitter: Submitter,
        *,
        logger: logging.Logger | None = None,
    ) -> None:
        self._config = config
        self._store = store
        self._executor = executor
        # The submitter is engine-scoped (engine=MAJORITY) but its `minimum` is the
        # smallest share count any window will ever submit. The runtime builds a
        # submitter with the maximum-of-the-windows so a 5s/20-share window and a
        # 90s/100-share window both fit. This constructor does not validate the
        # minimum — it is the caller's responsibility, because the minimum must be
        # built from a single value, not from a per-window tuple.
        self._submitter = submitter
        self._risk = RiskEngine()
        self._logger = logger
        self._states: dict[tuple[str, int], MajorityMarketState] = {}
        self._submitted: set[tuple[str, int]] = set()

    # ── market lifecycle ──────────────────────────────────────────────────────

    def open_market(self, slug: str, close_ts: int) -> None:
        """Create fresh state for every configured window of a newly opened market.

        A11: no reuse. Every state object is brand new, with the configured window
        in `execution_window_seconds` and an empty side lock.

        Markets with no configured windows get no state. They are reported OFF
        everywhere the engine is asked, and there is nothing to drop on close.
        """
        if not self._config.tradable:
            # Engine is OFF or fail-closed. Still register a state per window so
            # `state_for(window)` returns an honest OFF row, and drop_market is safe.
            for window in self._config.windows_by_offset:
                self._states[_window_key(slug, window.execution_window_seconds)] = (
                    MajorityMarketState(
                        market_slug=slug,
                        close_ts=close_ts,
                        execution_window_seconds=window.execution_window_seconds,
                        state=MajorityState.OFF,
                    )
                )
            return
        for window in self._config.tradable_windows:
            self._states[_window_key(slug, window.execution_window_seconds)] = (
                MajorityMarketState(
                    market_slug=slug,
                    close_ts=close_ts,
                    execution_window_seconds=window.execution_window_seconds,
                )
            )
        log_event(
            logging.DEBUG,
            "MAJORITY Market Opened",
            f"{slug}  windows="
            f"{[w.execution_window_seconds for w in self._config.tradable_windows]}",
            logger=self._logger,
        )

    def drop_market(self, slug: str) -> None:
        """Discard state for every window of a market. A11: thrown away, never reset.

        Drops ALL keys with this slug regardless of window, so a market with three
        configured windows leaves nothing behind when it closes.
        """
        for key in list(self._states):
            if key[0] == slug:
                self._states.pop(key, None)
                self._submitted.discard(key)

    def state_for(self, slug: str, window_seconds: int | None = None) -> MajorityMarketState | None:
        """Read-only snapshot for the dashboard. None if the market is not tracked.

        `window_seconds=None` returns the SINGLE-window state when there is exactly
        one configured window, else None. Callers that want a specific window
        pass its offset; multi-window dashboards always pass an explicit offset.
        """
        if window_seconds is None:
            if len(self._config.windows_by_offset) != 1:
                return None
            window_seconds = self._config.windows_by_offset[0].execution_window_seconds
        return self._states.get(_window_key(slug, window_seconds))

    def states_for_market(self, slug: str) -> tuple[MajorityMarketState, ...]:
        """Every per-window state for one market. Empty when the market is unknown.

        Ordered by window offset, so the deck renders them low-to-high without
        re-sorting.
        """
        rows = [s for (k0, _), s in self._states.items() if k0 == slug]
        return tuple(sorted(rows, key=lambda s: s.execution_window_seconds))

    @property
    def config(self) -> MajorityConfig:
        """The configuration this engine is running under.

        Exposed so the dashboard renders the values the ENGINE holds rather than
        re-reading `settings.majority`. The two are the same object at boot, but a
        Settings save builds a new config and swaps it in, and a deck reading the
        settings side would show values the running engine had rejected.

        Safe to hand out because MajorityConfig is frozen: a caller can read every
        field and change none of them.
        """
        return self._config

    # ── restart recovery ──────────────────────────────────────────────────────

    def restore_from_intents(self, slug: str, now: float) -> None:
        """Reconstruct locked-side state from persisted intents after a restart.

        Called once per market, when the runtime rediscovered an unsettled market
        and asked the engine to materialise its state. For every persisted
        MAJORITY intent on the market, this materialises the locked-side state via
        `reconstruct_locked_side`. The side lock is now in memory again and any
        subsequent attempt to re-determine it raises.

        Persistence-only state. Trigger timestamps, decision snapshots and verdict
        details are not on the intent (they are observation artifacts that died
        with the process); the deck shows the locked side and the order status
        reconstructed from the order row, which is enough to audit what was decided.
        """
        for intent in self._store.intents_for(slug, engine=MAJORITY_ENGINE):
            key = _window_key(slug, intent.offset_seconds)
            state = self._states.get(key)
            if state is None:
                # The market's window list changed between the original run and
                # this one. We still reconstruct the lock so the persisted order
                # is not orphaned by a swept sweep — but a fresh state is created
                # first so a window the operator removed is not silently re-armed.
                state = MajorityMarketState(
                    market_slug=slug,
                    close_ts=intent.close_ts,
                    execution_window_seconds=intent.offset_seconds,
                )
                self._states[key] = state
            state.reconstruct_locked_side(intent.direction, intent.created_at)
            self._submitted.add(key)
            log_event(
                logging.INFO,
                "MAJORITY Lock Restored",
                f"{slug}  {intent.offset_seconds}s  {intent.direction.value}  "
                f"intent={intent.intent_id}",
                logger=self._logger,
            )

    # ── main tick ─────────────────────────────────────────────────────────────

    async def tick(
        self,
        market: MarketInstance,
        health: RuntimeHealth,
        now: float,
    ) -> None:
        """Advance the MAJORITY sequence for one market, one tick.

        Advances EVERY configured window for the market. Windows whose state is
        terminal are skipped, so a window that already reached NO_TRADE in this
        run does nothing for the remainder of it.
        """
        if not self._config.tradable:
            return

        for window in self._config.tradable_windows:
            await self._tick_one(market, window, health, now)

    async def _tick_one(
        self,
        market: MarketInstance,
        window: MajorityWindowConfig,
        health: RuntimeHealth,
        now: float,
    ) -> None:
        """Advance ONE window for one market, one tick.

        Mirrors the single-window sequence (trigger → fresh read → submit), keyed
        by the window so two windows never share an evaluation. Returns when:
          - the engine is OFF / fail-closed (caller already filtered, but defensive)
          - no state exists for this (slug, window)
          - the window's MAJORITY sequence is already terminal
          - the market/window has already been submitted this cycle
        """
        key = _window_key(market.slug, window.execution_window_seconds)
        state = self._states.get(key)
        if state is None:
            return
        if state.terminal:
            return
        if key in self._submitted:
            return

        # ── 1. window open? ───────────────────────────────────────────────────
        window_open_at = market.close_ts - window.execution_window_seconds
        if now < window_open_at:
            # Window has not opened. Nothing to do this tick.
            return

        state.open_window()   # idempotent: only moves WAITING_WINDOW → WINDOW_OPEN

        # ── 2. trigger (step 1) ───────────────────────────────────────────────
        # Only evaluate when the trigger has not already fired. state.triggered is
        # set on the first firing and this block never runs again for the market.
        if not state.triggered:
            await self._evaluate_trigger(market, state, window, now)
            return  # let the runtime do one tick before the fresh read (step 2)

        # ── 3. fresh read and determination (step 2) ─────────────────────────
        # Only once: if we already have a verdict (MAJORITY_DETERMINED or later),
        # fall through to submission.
        if state.state is MajorityState.TRIGGERED:
            await self._fresh_read_and_determine(market, state, window, now)

        # ── 4. submission ─────────────────────────────────────────────────────
        if state.state is MajorityState.SIDE_SELECTED:
            await self._submit(market, state, window, health, now)

    # ── internal steps ────────────────────────────────────────────────────────

    async def _evaluate_trigger(
        self,
        market: MarketInstance,
        state: MajorityMarketState,
        window: MajorityWindowConfig,
        now: float,
    ) -> None:
        """Step 1: poll the book cache and test the trigger.

        Uses executor.best_price for both sides. These are the same calls the fresh
        read uses, so V1's PaperExecutor.best_price and V2's LiveExecutor.best_price
        are the only data sources — no mid-price, no asks, no cached interval data.

        Returns without marking anything if either side read fails (None result is
        treated as a missing bid, which is handled by is_triggered → False).
        """
        up = await self._executor.best_price(market.slug, Direction.UP)
        down = await self._executor.best_price(market.slug, Direction.DOWN)

        snapshot = BookSnapshot(
            best_bid_up=up,
            best_bid_down=down,
            # The caller's clock reading, never one taken here (A10/D1). Mixing a
            # monotonic reading into a field the freshness comparison uses would
            # subtract two different epochs and produce a meaningless age.
            read_at=now,
            fresh=True,  # just read; staleness is the caller's verdict
        )

        if is_triggered(snapshot, window.trigger_price):
            # Re-checked AFTER the two awaits above, which are the only points at
            # which a second tick could have reached this market and fired the
            # trigger already. Asked as a question rather than caught as an error:
            # mark_triggered raises on a second firing because a second firing means
            # a caller believes it may re-determine the side, and swallowing that
            # exception here would hide exactly the bug it exists to report.
            if state.triggered:
                return
            state.mark_triggered(snapshot, now)
            log_event(
                logging.INFO,
                "MAJORITY Triggered",
                f"{market.slug}  {window.execution_window_seconds}s  {snapshot.describe()}  "
                f"threshold {window.trigger_price}",
                logger=self._logger,
            )
        else:
            state.await_trigger()

    async def _fresh_read_and_determine(
        self,
        market: MarketInstance,
        state: MajorityMarketState,
        window: MajorityWindowConfig,
        now: float,
    ) -> None:
        """Step 2: read the book FRESH and determine which side to buy.

        This is a SEPARATE, INDEPENDENT read from the trigger read. The trigger
        snapshot and this snapshot are different objects: the trigger explains why
        the sequence started; this one explains which side is bought. Keeping both
        makes the two-step rule auditable rather than merely intended.

        A read older than _FRESH_MAX_AGE_SECONDS is marked stale, which causes
        determine_majority to return INDETERMINATE and the market to resolve NO_TRADE.
        """
        state.mark_reading()

        up = await self._executor.best_price(market.slug, Direction.UP)
        down = await self._executor.best_price(market.slug, Direction.DOWN)

        # Both readings come from the SAME clock — the caller's `now`, which is also
        # what mark_triggered recorded. A monotonic reading taken here would be
        # subtracted from a caller-clock reading, and the resulting "age" would be
        # the difference between two unrelated epochs: enormous, so every fresh read
        # would be judged stale and MAJORITY would never trade.
        triggered_at = state.triggered_at
        fresh = (
            triggered_at is not None and (now - triggered_at) <= _FRESH_MAX_AGE_SECONDS
        )
        snapshot = BookSnapshot(
            best_bid_up=up,
            best_bid_down=down,
            read_at=now,
            fresh=fresh,
        )

        verdict = determine_majority(snapshot)
        state.mark_determined(verdict, snapshot)

        log_event(
            logging.INFO,
            "MAJORITY Determined",
            f"{market.slug}  {window.execution_window_seconds}s  {snapshot.describe()}  "
            f"→ {verdict.outcome.value}  {verdict.reason}",
            logger=self._logger,
        )

        if verdict.tradable:
            try:
                state.select_side(verdict, snapshot, now)
            except MajorityStateError as exc:
                log_event(
                    logging.ERROR,
                    "MAJORITY Side Lock Failed",
                    str(exc),
                    logger=self._logger,
                )
                state.mark_no_trade(str(exc))
        else:
            state.mark_no_trade(verdict.reason)
            log_event(
                logging.INFO,
                "MAJORITY No Trade",
                f"{market.slug}  {window.execution_window_seconds}s  {verdict.reason}",
                logger=self._logger,
            )

    async def _submit(
        self,
        market: MarketInstance,
        state: MajorityMarketState,
        window: MajorityWindowConfig,
        health: RuntimeHealth,
        now: float,
    ) -> None:
        """Build intent, run risk gates, persist and submit.

        Mirrors the ordering in DecisionEngine.decide, and the ordering is the whole
        point:
          1. build intent (pure)
          2. evaluate risk gates
          3. save_intent (False means the UNIQUE constraint refused a second row)
          4. submit via Submitter

        Gates BEFORE the insert, never after. Gate 7 (duplicate_intent) reads
        `store.has_intent`, so persisting first would hand the gate the very row this
        call just wrote: every MAJORITY submission would deny itself as its own
        duplicate and no order would ever be placed. A4's write-before-act rule
        constrains the order of the insert and the VENUE CALL, which step 4 still
        honours; it says nothing about the gates, which are pure reads.

        QUOTA: MAJORITY allows at most one trade per market/window. The
        `_submitted` set enforces this without touching market.reservations. The
        insert at step 3 is the durable half of the same guarantee — it is what
        makes a restart refuse a second order for a market this process already
        traded.
        """
        key = _window_key(market.slug, window.execution_window_seconds)
        if key in self._submitted:
            return

        direction = state.selected_side
        if direction is None:
            # Should not happen — state is SIDE_SELECTED — but guard defensively.
            state.mark_no_trade("selected_side is None at submission time")
            return

        intent = ExecutionIntent(
            market_slug=market.slug,
            offset_seconds=window.execution_window_seconds,
            direction=direction,
            # MAJORITY has no signal TWAP or locked trigger in the TWAP sense.
            # These fields are CARRIED on ExecutionIntent for the TWAP path; MAJORITY
            # writes zero. They must be present because ExecutionIntent is shared
            # across engines — zero is an honest "not applicable" for a field that
            # TWAP computes and MAJORITY does not.
            signal_twap=_ZERO,
            locked_trigger=_ZERO,
            created_at=now,
            intent_id=majority_intent_id_for(market.slug, window.execution_window_seconds),
            trace_id=majority_trace_id_for(market.slug, window.execution_window_seconds),
            opening_twap=_ZERO,
            ptb=market.ptb if market.ptb is not None else _ZERO,
            buffer=window.buffer,
            limit_price=window.target_limit_price,
            size=window.shares,
            strategy_id=MAJORITY_ENGINE,
            close_ts=market.close_ts,
        )

        # ── risk gates ────────────────────────────────────────────────────────
        # Run against the intent as built, before anything is on disk. A proposal is
        # not an authorisation; the gates are what turn one into the other.
        context = self._risk_context(market, intent, window, health)
        verdict: RiskVerdict = self._risk.evaluate(context)

        if verdict.denied:
            log_event(
                logging.INFO,
                "MAJORITY Denied",
                f"{market.slug}  {window.execution_window_seconds}s  "
                f"{verdict.gate_id} {verdict.gate}  {verdict.reason}  {verdict.detail}",
                logger=self._logger,
            )
            state.mark_no_trade(
                f"{verdict.gate_id} {verdict.reason}: {verdict.detail}"
            )
            return

        # Persist before the venue call (write-before-act, A4). save_intent returns
        # False when the UNIQUE constraint fires — another pass, or the process
        # before this one, already recorded this window. SQLite arbitrates, and the
        # correct outcome is "already done", not an error: gate 7 above cannot see a
        # row committed after it read.
        if not self._store.save_intent(intent, engine=MAJORITY_ENGINE):
            log_event(
                logging.INFO,
                "MAJORITY Intent Duplicate",
                f"{market.slug}  {window.execution_window_seconds}s  "
                "already persisted, skipping submission",
                logger=self._logger,
            )
            state.mark_intent_created()  # bring state in sync
            self._submitted.add(key)
            return

        state.mark_intent_created()

        # Mark submitted before the venue call: if the process dies between here
        # and the call completing, the persisted intent above is what makes the
        # restart refuse a second order.
        self._submitted.add(key)

        try:
            placed = await self._submitter.submit(
                intent,
                count=1,
                phase=market.phase,
                now=now,
            )
        except MarketPhaseError as exc:
            # Market closed between the gate check and the submit call.
            log_event(
                logging.WARNING,
                "MAJORITY Submission Skipped",
                f"{market.slug}  {window.execution_window_seconds}s  {exc}",
                logger=self._logger,
            )
            state.mark_no_trade(str(exc))
            return

        if placed:
            state.mark_state(MajorityState.SUBMITTED)
            log_event(
                logging.INFO,
                "MAJORITY Submitted",
                f"{market.slug}  {window.execution_window_seconds}s  "
                f"{direction.value}  {window.target_limit_price}  {len(placed)} order(s)",
                logger=self._logger,
            )
        else:
            # Submitter split produced zero orders (size below minimum). Already
            # logged by the Submitter; record NO_TRADE here so the state is terminal.
            state.mark_no_trade("size split produced no orders (below exchange minimum)")

    # ── risk context ──────────────────────────────────────────────────────────

    def _risk_context(
        self,
        market: MarketInstance,
        intent: ExecutionIntent,
        window: MajorityWindowConfig,
        health: RuntimeHealth,
    ) -> RiskContext:
        """Gather every gate's input from MAJORITY's perspective.

        Notable differences from the TWAP risk context:
          - window_triggered: taken from has_intent (the trigger already fired —
            we would not be here otherwise), but G04 just needs True.
          - strategy_enabled / strategy_id: MAJORITY is not a Strategy; the gate
            is passed with strategy_enabled=True so it does not deny, and the id
            is the engine name for the log line.
          - quota: max_trades_per_market=1, used=0 if no intent yet (this call
            happens after save_intent, so used=0 / reserved=0 is correct pre-fill).
          - intent_exists: checked against the MAJORITY engine specifically.
          - ptb: the market's frozen PTB (G05 requires a positive value).
          - entry band: this window's own entry_price_min/max.
        """
        limits = _limits_from_window(window)
        return RiskContext(
            # ── gate 1 ────────────────────────────────────────────────────────
            trading_enabled=health.trading_enabled,
            spec_status=health.spec_status,
            execution_armed=health.execution_armed,
            paused=health.paused,
            trading_disabled_reason=health.trading_disabled_reason,
            # ── gate 3 ────────────────────────────────────────────────────────
            phase=market.phase,
            # ── gate 4: the MAJORITY trigger has fired; that is why we are here
            window_triggered=True,
            # ── gate 5: the market's official PTB ────────────────────────────
            ptb=market.ptb,
            # ── gate 6: MAJORITY is not a Strategy; pass the gate ────────────
            strategy_enabled=True,
            strategy_id=MAJORITY_ENGINE,
            # ── gate 7: duplicate intent check, engine-scoped ─────────────────
            intent_exists=self._store.has_intent(
                market.slug,
                window.execution_window_seconds,
                engine=MAJORITY_ENGINE,
            ),
            # ── gate 8: MAJORITY quota ────────────────────────────────────────
            # At most one trade per market/window. We just saved the intent (or it
            # already existed). Used=0, reserved=0, limit=1 → committed=0 < 1 →
            # ALLOWED. If intent_exists=True above, G07 will fire first.
            quota_used=0,
            quota_reserved=0,
            max_trades_per_market=limits.max_trades_per_market,
            # ── gate 9 ────────────────────────────────────────────────────────
            direction=intent.direction,
            directions_held=market.directions_held(),
            allow_opposing_directions=limits.allow_opposing_directions,
            # ── gate 10 ───────────────────────────────────────────────────────
            open_positions=health.open_positions,
            max_concurrent_positions=limits.max_concurrent_positions,
            # ── gates 11 and 12 ───────────────────────────────────────────────
            limit_price=intent.limit_price,
            size=intent.size,
            entry_price_min=limits.entry_price_min,
            entry_price_max=limits.entry_price_max,
            min_tradable_size=limits.min_tradable_size,
            # ── gate 13: loss limits disabled for MAJORITY (zero → skip) ──────
            daily_loss_usd=health.daily_loss_usd,
            consecutive_losses=health.consecutive_losses,
            max_daily_loss_usd=limits.max_daily_loss_usd,
            max_consecutive_losses=limits.max_consecutive_losses,
            # ── gates 14-19 ───────────────────────────────────────────────────
            feed_blocked=health.feed_blocked,
            feed_age_ms=health.feed_age_ms,
            clock_drift_critical=health.clock_drift_critical,
            clock_drift_ms=health.clock_drift_ms,
            runtime_healthy=health.healthy,
            runtime_detail=health.detail,
            supervisor_ready=health.supervisor_ready,
            supervisor_detail=health.supervisor_detail,
            wallet_connected=health.wallet_connected,
            wallet_status=health.wallet_status,
            orphan_orders=health.orphan_orders,
            available_balance=health.available_balance,
            trace_id=majority_trace_id_for(
                market.slug, window.execution_window_seconds
            ),
        )
