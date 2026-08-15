"""MAJORITY per-market engine: entry condition, fresh read, side selection, intent, submission.

One instance per process. Keyed internally by (market_slug, window_seconds) so a
market with N configured windows holds N independent state objects. Per-market state
objects are created when a market opens and DROPPED when it closes — the same A11
discipline MarketInstance follows. There is no reset(), clear() or reuse path.

THE DIVISION OF LABOUR (spec §6-§10, final spec §5-§9)
======================================================
The BUFFER switch decides whether the buffer entry condition is active at all:
OFF means entry never waits on BTC or TWAP movement. Active, the window's ENTRY
MODE decides WHEN the opportunity fires:
  DIRECT        buffer condition absent: fires at window open. No BTC ± 0
                waiting, no trigger mathematics at all (§8, final spec §9).
  BTC_TRIGGER   window > 30s with buffer ON > 0: at window open the current BTC
                spot is captured as the reference; UP_TRIGGER = ref + buffer,
                DOWN_TRIGGER = ref - buffer. The first level the live spot
                satisfies opens the opportunity (§6). The levels live in
                memory only — they are never orders and never Polymarket
                limit prices.
  TWAP_SUPPORT  window ≤ 30s with buffer ON: the running TWAP reference
                supports the entry — the condition |signal TWAP - PTB| ≥
                buffer opens the opportunity (§9). TWAP is SUPPORT DATA: it
                gates when, it never decides which side.

WHICH side is traded is ALWAYS the MAJORITY decision: after the entry
condition fires, a FRESH book read is taken and the side with the higher
fresh bid wins (STRICT — equal or missing bids are INDETERMINATE → NO_TRADE).
The trigger that fired first does NOT force the direction: an UP trigger can
fire and MAJORITY can still select DOWN, and the DOWN order is correct (§6).

THE COMBINED SWITCH (spec §10, final spec §5/§12)
=================================================
One switch controls the editable trigger + target limit price together:
  ON  → the window first waits for the configured Polymarket trigger price to
        be reached (latched once), then evaluates the buffer condition, then
        submits at the configured TARGET LIMIT PRICE (band-gated, risk-gated).
        The trigger is a WHEN gate only — the fresh read after the fire still
        decides which side.
  OFF → the trigger price is validated but never waited on; the window trades
        the MAJORITY direction at the currently valid market price: the
        majority side's live best bid, quantized to the venue tick and bounded
        by the entry band and every risk gate.
The switch decides the PRICE, never the direction.

THE FINAL DIRECTION GATE (spec §13)
===================================
Before EVERY submission, `_gate_direction` re-verifies 12 checks — market id,
window id, engine identity, decision evidence, locked side, direction match,
token/price mapping, price validity, quantity validity, risk verdict, data
freshness, duplicate absence. Any failure refuses the submission and records
which check failed. The gate never corrects a mismatched direction; it
refuses it.

QUOTA ISOLATION
===============
MAJORITY keeps its own submitted accounting and never touches
`market.reservations` (the shared set TWAP used). At most one trade per
market/window ever, enforced in memory by `_submitted` and durably by the
intent table's UNIQUE constraint.

PERSISTENT SIDE LOCK
====================
A side lock decided by `select_side` lives only in memory. A restart loses it.
Reconstruction reads the persisted ExecutionIntent for each (market, window)
and materialises the locked-side state via `reconstruct_locked_side`, so a
restarted process resumes a locked-side market without re-reading the book.

GATE 5 (price_to_beat)
======================
RiskContext requires a positive PTB. MAJORITY never compares against it, but
the gate runs for every engine. `market.ptb` is passed through unchanged: it
is either already frozen (normal case) or None (pre-freeze edge), in which
case G05 denies and the window waits for the next tick.
"""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Final

from arc.domain.enums import Direction
from arc.domain.health import RuntimeHealth
from arc.domain.models import ExecutionIntent, MarketInstance
from arc.domain.money import quantize_price
from arc.errors import MarketPhaseError
from arc.execution.protocol import Executor
from arc.execution.submit import Submitter
from arc.logging_setup import log_event
from arc.majority.config import (
    MAJORITY_ENGINE,
    EntryMode,
    MajorityConfig,
    MajorityWindowConfig,
)
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
        "_tick_size",
    )

    def __init__(
        self,
        config: MajorityConfig,
        store: Store,
        executor: Executor,
        submitter: Submitter,
        *,
        tick_size: Decimal,
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
        # The venue's price increment (spec §11). Used only to validate the
        # submission price in the final direction gate — never to reprice.
        self._tick_size = tick_size
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
            state = MajorityMarketState(
                market_slug=slug,
                close_ts=close_ts,
                execution_window_seconds=window.execution_window_seconds,
            )
            state.entry_mode = EntryMode.for_window(
                window, buffer_enabled=self._config.buffer_enabled
            ).value
            self._states[_window_key(slug, window.execution_window_seconds)] = state
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
        configured windows leaves nothing behind when it closes. Windows that close
        still sitting before an intent are reported once: they are an entry
        opportunity that expired, which is an engine outcome, not an error.
        """
        expired = sorted(
            s.execution_window_seconds
            for (k0, _), s in self._states.items()
            if k0 == slug and not s.terminal and s.state in _EXPIRABLE_STATES
        )
        if expired:
            log_event(
                logging.INFO,
                "MAJORITY Windows Expired",
                f"{slug}  no entry on {', '.join(f'{o}s' for o in expired)}",
                logger=self._logger,
            )
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

        Sequence: entry condition (§6/§8/§9) → fresh read → MAJORITY decision →
        direction lock → final gate → intent → submission. Returns when:
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

        # ── 2. entry condition ────────────────────────────────────────────────
        # Only evaluate when the trigger has not already fired. state.triggered is
        # set on the first firing and this block never runs again for the window.
        if not state.triggered:
            await self._evaluate_entry(market, state, window, now)
            return  # the fresh read happens on the NEXT tick, so the book it
            # reads is a distinct, later read than anything the trigger saw

        # ── 3. fresh read and MAJORITY determination ──────────────────────────
        # Only once: if we already have a verdict (MAJORITY_DETERMINED or later),
        # fall through to submission.
        if state.state is MajorityState.TRIGGERED:
            await self._fresh_read_and_determine(market, state, window, now)

        # ── 4. submission ─────────────────────────────────────────────────────
        if state.state is MajorityState.SIDE_SELECTED:
            await self._submit(market, state, window, health, now)

    # ── internal steps ────────────────────────────────────────────────────────

    async def _evaluate_entry(
        self,
        market: MarketInstance,
        state: MajorityMarketState,
        window: MajorityWindowConfig,
        now: float,
    ) -> None:
        """Evaluate the window's entry condition (spec §6/§8/§9, final spec
        §5-§13). Async: the trigger gate reads the book directly.

        The entry condition decides WHEN the opportunity fires — never WHICH side
        is traded. That is the fresh book read's job in the next step. The order
        of the conditions is fixed by final spec §12 — no order may be placed
        before ALL configured conditions are satisfied — so the two gates run in
        this order, and each returns without the next when its condition is not
        met:

          1. trigger gate (trigger/target switch ON): the configured Polymarket
             trigger price must be reached first. Latched once reached; the book
             moving back through it cannot un-fire it.
          2. buffer gate (BUFFER switch ON): the window's buffer condition —
             BTC ± buffer for windows > 30s, |signal TWAP - PTB| ≥ buffer for
             windows ≤ 30s — all memory-only entry conditions, never orders and
             never Polymarket limit prices.

        No order is submitted from here; no Polymarket price is derived from a
        BTC level.
        """
        # ── 1. trigger gate (final spec §10-§12) ──────────────────────────────
        # The trigger price is compared against the market's best bid — the same
        # value MAJORITY's determination reads. `is_triggered` is inclusive and
        # requires a fresh, complete read; an unusable or unreached read leaves
        # the window waiting.
        if self._config.trigger_limit_enabled and not state.price_trigger_reached:
            best_bid_up = await self._executor.best_price(market.slug, Direction.UP)
            best_bid_down = await self._executor.best_price(market.slug, Direction.DOWN)
            snapshot = BookSnapshot(
                best_bid_up=best_bid_up,
                best_bid_down=best_bid_down,
                read_at=now,
                fresh=True,
            )
            if is_triggered(snapshot, window.trigger_price):
                state.price_trigger_reached = True
                log_event(
                    logging.INFO,
                    "MAJORITY Price Trigger Reached",
                    f"{market.slug}  {window.execution_window_seconds}s  "
                    f"trigger={window.trigger_price}  book={snapshot.describe()}",
                    logger=self._logger,
                )
            else:
                state.await_trigger()
                return

        # ── 2. buffer gate (final spec §5-§9) ─────────────────────────────────
        # BUFFER OFF → DIRECT, whatever the stored buffer value says: the value
        # is not an entry condition while the switch is OFF.
        mode = EntryMode.for_window(window, buffer_enabled=self._config.buffer_enabled)

        if mode is EntryMode.DIRECT:
            # §8 / final spec §9: no buffer condition. Never wait for BTC + 0 /
            # BTC - 0 — the MAJORITY direction is taken at the best valid
            # limit-order price available.
            self._fire(state, window, now, fired_level=None, fired_spot=None)
            return

        if mode is EntryMode.BTC_TRIGGER:
            # §6: capture the BTC reference ONCE at window open, then monitor
            # ref ± buffer. No reference yet (no observation since open) → wait.
            if state.btc_reference is None:
                spot = market.last_btc
                if spot is None:
                    state.await_trigger()
                    return
                state.btc_reference = spot
                state.btc_up_trigger = spot + window.buffer
                state.btc_down_trigger = spot - window.buffer
                log_event(
                    logging.DEBUG,
                    "MAJORITY Trigger Levels Set",
                    f"{market.slug}  {window.execution_window_seconds}s  "
                    f"ref={spot}  up={state.btc_up_trigger}  "
                    f"down={state.btc_down_trigger}",
                    logger=self._logger,
                )
            spot = market.last_btc
            if spot is None:
                state.await_trigger()
                return
            # Inclusive, and UP first: the levels are symmetric around a single
            # spot, so both cannot be satisfied by one observation unless the
            # buffer is zero — which is the DIRECT mode, handled above.
            if state.btc_up_trigger is not None and spot >= state.btc_up_trigger:
                self._fire(state, window, now, fired_level=state.btc_up_trigger, fired_spot=spot)
            elif state.btc_down_trigger is not None and spot <= state.btc_down_trigger:
                self._fire(state, window, now, fired_level=state.btc_down_trigger, fired_spot=spot)
            else:
                state.await_trigger()
            return

        # §9 TWAP_SUPPORT: window ≤ 30s. The running TWAP reference supports the
        # entry; |signal TWAP - PTB| ≥ buffer is the direction-agnostic timing
        # gate. PTB frozen but no observations yet → signal_twap is None → wait.
        ptb = market.ptb
        twap = market.signal_twap
        if ptb is None or twap is None:
            state.await_trigger()
            return
        if abs(twap - ptb) >= window.buffer:
            self._fire(state, window, now, fired_level=None, fired_spot=None)
        else:
            state.await_trigger()

    def _fire(
        self,
        state: MajorityMarketState,
        window: MajorityWindowConfig,
        now: float,
        *,
        fired_level: Decimal | None,
        fired_spot: Decimal | None,
    ) -> None:
        """Record that the entry condition is satisfied. Fires exactly once.

        The trigger snapshot for non-BTC modes carries no book data at all — the
        entry condition was a BTC-spot or TWAP comparison, and inventing book
        bids for it would make the trigger evidence lie about what fired it.
        """
        if state.triggered:
            return
        state.fired_level = fired_level
        state.fired_spot = fired_spot
        snapshot = BookSnapshot(
            best_bid_up=None,
            best_bid_down=None,
            # The caller's clock reading, never one taken here (A10/D1). The
            # freshness comparison in step 2 subtracts two readings of this same
            # clock; mixing epochs would make every fresh read look stale.
            read_at=now,
            fresh=True,
        )
        state.mark_triggered(snapshot, now)
        evidence = (
            f"level={fired_level} spot={fired_spot}"
            if fired_level is not None
            else "direct entry"
        )
        log_event(
            logging.INFO,
            "MAJORITY Triggered",
            f"{state.market_slug}  {window.execution_window_seconds}s  "
            f"mode={state.entry_mode}  {evidence}",
            logger=self._logger,
        )

    async def _fresh_read_and_determine(
        self,
        market: MarketInstance,
        state: MajorityMarketState,
        window: MajorityWindowConfig,
        now: float,
    ) -> None:
        """Read the book FRESH and let MAJORITY determine which side to buy.

        This is a SEPARATE, INDEPENDENT read from whatever satisfied the entry
        condition — for a BTC trigger it is the first book read of the sequence
        at all. The trigger snapshot and this snapshot are different evidence:
        the trigger explains why the sequence started, this one explains which
        side is bought. Keeping both makes the two-step rule auditable.

        A determination older than _FRESH_MAX_AGE_SECONDS from the trigger is
        marked stale, which causes determine_majority to return INDETERMINATE
        and the window to resolve NO_TRADE.
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
        """Build intent, run risk gates, pass the direction gate, persist, submit.

        Ordering is the whole point:
          1. price the order (switch-dependent, §10)
          2. build intent (pure)
          3. evaluate risk gates
          4. final 12-check direction gate (§13) — any failure refuses submission
          5. save_intent (False means the UNIQUE constraint refused a second row)
          6. submit via Submitter

        Gates BEFORE the insert, never after. Gate 7 (duplicate_intent) reads
        `store.has_intent`, so persisting first would hand the gate the very row this
        call just wrote: every MAJORITY submission would deny itself as its own
        duplicate and no order would ever be placed. A4's write-before-act rule
        constrains the order of the insert and the VENUE CALL, which step 6 still
        honours; it says nothing about the gates, which are pure reads.
        """
        key = _window_key(market.slug, window.execution_window_seconds)
        if key in self._submitted:
            return

        direction = state.selected_side
        if direction is None:
            # Should not happen — state is SIDE_SELECTED — but guard defensively.
            state.mark_no_trade("selected_side is None at submission time")
            return

        # ── price the order (§10) ─────────────────────────────────────────────
        # Switch ON  → the configured target limit price.
        # Switch OFF → the currently valid market price for the MAJORITY side:
        #              the fresh decision read's best bid for that side, quantized
        #              to the venue tick. An unreadable or out-of-band price is a
        #              refusal, never a fallback to another price.
        if self._config.trigger_limit_enabled:
            limit_price = window.target_limit_price
        else:
            live_price = self._live_entry_price(state, direction)
            if live_price is None:
                state.mark_no_trade(
                    "switch OFF: no valid live price for the MAJORITY direction"
                )
                log_event(
                    logging.INFO,
                    "MAJORITY No Trade",
                    f"{market.slug}  {window.execution_window_seconds}s  "
                    "live entry price unreadable or outside the entry band",
                    logger=self._logger,
                )
                return
            limit_price = live_price

        intent = ExecutionIntent(
            market_slug=market.slug,
            offset_seconds=window.execution_window_seconds,
            direction=direction,
            # MAJORITY has no signal TWAP or locked trigger in the TWAP sense.
            # signal_twap/opening_twap are CARRIED as honest zeros (not
            # applicable); locked_trigger carries the BTC level that opened the
            # opportunity when one exists, and zero when the entry was direct.
            signal_twap=_ZERO,
            locked_trigger=state.fired_level if state.fired_level is not None else _ZERO,
            created_at=now,
            intent_id=majority_intent_id_for(market.slug, window.execution_window_seconds),
            trace_id=majority_trace_id_for(market.slug, window.execution_window_seconds),
            opening_twap=_ZERO,
            ptb=market.ptb if market.ptb is not None else _ZERO,
            buffer=window.buffer,
            limit_price=limit_price,
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

        # ── the final direction gate (spec §13) ───────────────────────────────
        # The last boundary before the venue. Re-verifies the whole proposal —
        # identity, direction, price, quantity, risk, freshness, duplicates —
        # and refuses on ANY failure. It never corrects a mismatch; it refuses.
        gate_failure = self._gate_direction(market, state, window, intent, verdict, key)
        if gate_failure is not None:
            log_event(
                logging.WARNING,
                "MAJORITY Direction Gate Failed",
                f"{market.slug}  {window.execution_window_seconds}s  {gate_failure}",
                logger=self._logger,
            )
            state.mark_no_trade(f"direction gate: {gate_failure}")
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
        log_event(
            logging.INFO,
            "MAJORITY Intent Created",
            f"{market.slug}  {window.execution_window_seconds}s  "
            f"intent={intent.intent_id}",
            logger=self._logger,
        )

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
                f"{direction.value}  {limit_price}  {len(placed)} order(s)",
                logger=self._logger,
            )
        else:
            # Submitter split produced zero orders (size below minimum). Already
            # logged by the Submitter; record NO_TRADE here so the state is terminal.
            state.mark_no_trade("size split produced no orders (below exchange minimum)")

    # ── the final direction gate (spec §13) ───────────────────────────────────

    def _gate_direction(
        self,
        market: MarketInstance,
        state: MajorityMarketState,
        window: MajorityWindowConfig,
        intent: ExecutionIntent,
        risk: RiskVerdict,
        key: tuple[str, int],
    ) -> str | None:
        """The 12-check final execution boundary. Returns None when all pass,
        otherwise the name of the first check that failed.

        Runs before EVERY submission (V1 and V2 alike — parity mandates one
        logic). It is the last gate the proposal passes; a failure here is a
        refusal, never an auto-correction. Correcting a mismatched direction
        would mean the engine trading a side MAJORITY never chose.
        """
        # 1. market identity
        if intent.market_slug != market.slug:
            return "market id mismatch"
        # 2. window identity
        if intent.offset_seconds != window.execution_window_seconds:
            return "window id mismatch"
        if state.execution_window_seconds != window.execution_window_seconds:
            return "state window mismatch"
        # 3. engine identity
        if intent.strategy_id != MAJORITY_ENGINE:
            return "engine identity mismatch"
        # 4. a MAJORITY decision exists
        if state.verdict is None or state.decision_snapshot is None:
            return "no MAJORITY decision on record"
        # 5. a locked direction exists
        if not state.side_locked or state.selected_side is None:
            return "no locked direction"
        # 6. requested order direction == locked MAJORITY direction
        if intent.direction != state.selected_side:
            return "order direction != locked MAJORITY direction"
        # 7. market/token mapping: the decision book carries a usable bid for
        # the chosen side. UP shares are bought against the UP bid, DOWN against
        # the DOWN bid — a missing price means the mapping cannot be verified.
        # Completeness only, NOT freshness: `usable` folds both in, and a stale
        # read refused here would be reported as a missing bid. Freshness is
        # check 11's job, and it must be reachable to be verifiable.
        snapshot = state.decision_snapshot
        side_bid = (
            snapshot.best_bid_up
            if intent.direction is Direction.UP
            else snapshot.best_bid_down
        )
        if not snapshot.complete or side_bid is None:
            return "decision book has no usable bid for the chosen side"
        # 8. price valid: positive and on the venue's exact tick grid
        if intent.limit_price <= _ZERO:
            return "price not positive"
        if quantize_price(intent.limit_price, self._tick_size) != intent.limit_price:
            return "price not on the venue tick grid"
        # 9. quantity valid: positive, exactly the configured size, ≥ venue minimum
        if intent.size <= _ZERO or intent.size != window.shares:
            return "quantity invalid"
        # 10. risk permits this exact execution
        if risk.denied:
            return f"risk denied: {risk.gate_id} {risk.reason}"
        # 11. data fresh: the decision read is a fresh one, and the feed gates
        # (which the risk layer already evaluated) all passed
        if not snapshot.fresh:
            return "decision read is stale"
        # 12. no duplicate order exists — in memory or durably
        if key in self._submitted:
            return "duplicate submission in this run"
        if self._store.has_intent(
            market.slug, window.execution_window_seconds, engine=MAJORITY_ENGINE
        ):
            return "duplicate intent already persisted"
        return None

    def _live_entry_price(
        self, state: MajorityMarketState, direction: Direction
    ) -> Decimal | None:
        """The switch-OFF submission price: the MAJORITY side's live best bid.

        Taken from the fresh decision read (the only book this sequence has
        read), quantized to the venue tick and bounded by the entry band. None
        means no valid live price exists — the caller refuses rather than
        substituting another price.
        """
        snapshot = state.decision_snapshot
        if snapshot is None or not snapshot.fresh or not snapshot.usable:
            return None
        bid = (
            snapshot.best_bid_up if direction is Direction.UP else snapshot.best_bid_down
        )
        if bid is None:
            return None
        price = quantize_price(bid, self._tick_size)
        if price <= _ZERO:
            return None
        window = self._config.window_for(state.execution_window_seconds)
        if window is None:
            return None
        if price < window.entry_price_min or price > window.entry_price_max:
            return None
        return price

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


# States a window can still be in when its market closes: nothing persisted, nothing
# refused, nothing that already reported itself. Reaching an intent means the window
# acted; terminal states mean it already spoke. What remains is an expired entry.
_EXPIRABLE_STATES: Final[frozenset[MajorityState]] = frozenset(
    {
        MajorityState.WAITING_WINDOW,
        MajorityState.WINDOW_OPEN,
        MajorityState.WAITING_TRIGGER,
        MajorityState.TRIGGERED,
        MajorityState.READING_CLOB,
        MajorityState.MAJORITY_DETERMINED,
        MajorityState.SIDE_SELECTED,
    }
)
