"""The Decision Engine. The ONLY creator of ExecutionIntent objects.

One pipeline, and every intent goes through all six steps:

    1  Completed Window   a FIRED window, taken from the window pass
    2  Validate Window    frozen state is complete; snapshot it once
    3  Apply Risk Gates   all nineteen, in order, first denial wins
    4  Create Intent      immutable and self-sufficient
    5  Persist Intent     SQLite arbitrates exactly-one-per-window
    6  Return Intent

The engine submits nothing. It has no venue client, no wallet, no key, no HTTP
session and no cancel path. Its output is a row and an object.

Multi-trade behaviour is derived from `max_trades_per_market` rather than from a
separate flag: a per-market limit of one IS single-trade mode. A second boolean
could disagree with the limit — "multi trade enabled, max trades 1" — and there
would be no correct reading of that configuration.

    multi-trade   every fired window is independent and may produce one intent
    single-trade  priority 3 -> 5 -> 7 -> 10 -> 15; the first success wins and
                  the remaining windows are skipped as LOWER_PRIORITY

Nothing here reads a clock to decide admissibility. `now` is passed in and used
only as the recorded `created_at`. The lead-time gate is repealed entirely (A10/D1).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Final

from arc.decision.intent import build_intent, trace_id_for
from arc.decision.quota import QuotaLedger
from arc.decision.reasons import SkipReason
from arc.decision.snapshot import DecisionSnapshot, snapshot_for
from arc.decision.strategy import context_for
from arc.domain.enums import DenialReason, Direction, SettlementSpecStatus, WindowState
from arc.domain.models import ExecutionIntent, ExecutionWindow, MarketInstance
from arc.domain.money import dec_str
from arc.logging_setup import log_event
from arc.risk.engine import RiskContext, RiskEngine
from arc.risk.limits import RiskLimits
from arc.storage.store import Store
from arc.strategy.config import StrategyConfig
from arc.strategy.registry import StrategyRegistry

__all__ = [
    "DecisionEngine",
    "DecisionOutcome",
    "QuoteSource",
    "RuntimeHealth",
    "WindowDecision",
]

_ZERO: Final[Decimal] = Decimal("0")

# A book price for one side of one market. Returns None when no usable quote is
# available, which is a skip rather than a zero: a zero would divide the budget by
# nothing and produce an absurd size.
QuoteSource = Callable[[str, Direction], "Decimal | None"]


@dataclass(frozen=True, slots=True)
class RuntimeHealth:
    """Process-wide state the risk gates need, read once per decision pass.

    Gathered by the caller into one frozen object rather than reached for gate by
    gate. Nineteen gates each pulling live readings would evaluate nineteen
    slightly different worlds, and the verdict would depend on how long evaluation
    took.
    """

    trading_enabled: bool
    spec_status: SettlementSpecStatus
    # The operator's Start Trading switch. Defaults False for the same reason the
    # risk gate does: a caller that forgets to gather it records the decision and
    # submits nothing, rather than submitting because a field was missing.
    execution_armed: bool = False
    paused: bool = False
    trading_disabled_reason: str = ""
    feed_blocked: bool = False
    feed_age_ms: float | None = None
    clock_drift_critical: bool = False
    clock_drift_ms: float = 0.0
    healthy: bool = True
    detail: str = ""
    open_positions: int = 0
    daily_loss_usd: Decimal = _ZERO
    consecutive_losses: int = 0
    # ── the live-money preconditions (gates 16-19) ───────────────────────────
    # Each defaults to the value that means "nothing is wrong", because that is
    # what is true of every caller that does not have a venue: V1, the inert
    # runtime and every unit test. Gate 2's arming switch defaults the other way
    # on purpose — it is the operator's intent, and absence of intent is not
    # consent — but absence of an orphan is genuinely the absence of an orphan.
    supervisor_ready: bool = True
    supervisor_detail: str = ""
    wallet_connected: bool = True
    wallet_status: str = ""
    orphan_orders: tuple[str, ...] = ()
    # None = no official source published a balance. Never zero as a stand-in:
    # zero is a real, denying figure and "unknown" must not be able to look like
    # an empty account.
    available_balance: Decimal | None = None
    # Which runtime produced this pass. Carried so a denial line says whether the
    # refusal happened in V1 or V2 — the same denial means different things in a
    # paper run and a live one.
    mode: str = ""
    # Bumped by the runtime whenever any field above changes. The dashboard
    # redraws on a change of this number rather than on every frame.
    health_revision: int = 0


@dataclass(frozen=True, slots=True)
class WindowDecision:
    """The outcome for ONE window. Exactly one of intent / denial / skip is set.

    All three are carried on one type so the caller cannot handle intents and
    forget denials: a denial that is silently dropped is a trade the operator
    believes happened.
    """

    offset_seconds: int
    intent: ExecutionIntent | None = None
    denial: DenialReason | None = None
    skip: SkipReason | None = None
    gate: str = ""
    detail: str = ""

    @property
    def acted(self) -> bool:
        return self.intent is not None


@dataclass(frozen=True, slots=True)
class DecisionOutcome:
    """What one decision pass over one market produced."""

    market_slug: str
    decisions: tuple[WindowDecision, ...] = field(default=())

    @property
    def intents(self) -> tuple[ExecutionIntent, ...]:
        return tuple(d.intent for d in self.decisions if d.intent is not None)

    @property
    def denials(self) -> tuple[WindowDecision, ...]:
        return tuple(d for d in self.decisions if d.denial is not None)

    @property
    def acted(self) -> bool:
        return bool(self.intents)


def _skip_for(state: WindowState) -> SkipReason:
    """Why a window that is not FIRED produced nothing.

    NO_DIRECTION is reported as itself rather than as NOT_FROZEN. Both are true of the
    window, but they mean opposite things to an operator: NOT_FROZEN is a window still
    waiting for its instant (or one whose freeze was rejected and will be retried),
    while NO_DIRECTION is a final strategy verdict — the frozen TWAP equalled the
    official PTB, so strict comparison yielded no side to trade. Collapsing them would
    make a deliberate no-trade look like a stalled window.
    """
    if state is WindowState.NO_DIRECTION:
        return SkipReason.NO_DIRECTION
    if state is WindowState.PENDING:
        return SkipReason.NOT_FIRED
    if state in (WindowState.FROZEN, WindowState.FIRED):
        return SkipReason.NOT_FIRED
    return SkipReason.NOT_FROZEN


class DecisionEngine:
    """Turns fired windows into persisted ExecutionIntents, or into a named refusal.

    Holds no per-market state. Every per-market value comes from the MarketInstance
    passed in, so one engine serves both markets alive across a close boundary
    (A11/D6) and there is no cache that could carry one market's decision into the
    next. The counters are process-level totals for the dashboard.
    """

    __slots__ = (
        "_health",
        "_limits",
        "_logger",
        "_quota",
        "_quote",
        "_registry",
        "_risk",
        "_store",
        "_strategy_config",
        "intents_created",
        "intents_denied",
        "intents_skipped",
    )

    def __init__(
        self,
        store: Store,
        *,
        strategy_config: StrategyConfig,
        limits: RiskLimits,
        registry: StrategyRegistry,
        quota: QuotaLedger,
        quote_source: QuoteSource,
        health_source: Callable[[], RuntimeHealth],
        risk: RiskEngine | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self._store = store
        self._strategy_config = strategy_config
        # Snapshotted at construction rather than re-read per gate, for the same
        # reason RuntimeHealth is: a live config swap mid-pass would change policy
        # between two gates of a single decision.
        self._limits = limits
        self._registry = registry
        self._quota = quota
        self._quote = quote_source
        self._health = health_source
        self._risk = risk if risk is not None else RiskEngine()
        self._logger = logger
        self.intents_created = 0
        self.intents_denied = 0
        self.intents_skipped = 0

    @property
    def limits(self) -> RiskLimits:
        """The limits snapshotted at construction. Read-only."""
        return self._limits

    @property
    def strategy_count(self) -> int:
        """How many strategies are registered. Gate 6's standing input."""
        return len(self._registry)

    # ── the pass ─────────────────────────────────────────────────────────────

    def decide(self, market: MarketInstance, now: float) -> DecisionOutcome:
        """Decide every fired window on one market. Level-triggered, idempotent.

        Idempotent because a window that already produced an intent is refused by
        the duplicate gate, so calling this repeatedly on the same market cannot
        produce a second intent for the same window (A12). That is what makes it
        safe to call from the window-pass path at whatever cadence it runs.
        """
        health = self._health()
        decisions: list[WindowDecision] = []
        single_trade = self._quota.limit <= 1
        already_acted = False

        # Ascending offset: 3, 5, 7, 10, 15. The nearest window is the
        # best-informed one, so in single-trade mode it must get the first refusal
        # of the budget rather than losing it to a window that fired earlier.
        for window in market.windows_by_priority():
            if window.state is not WindowState.FIRED:
                decisions.append(
                    WindowDecision(
                        offset_seconds=window.offset_seconds,
                        skip=_skip_for(window.state),
                    )
                )
                continue

            if single_trade and already_acted:
                # Not a risk denial: the budget was spent by a nearer window, which
                # is the configured behaviour rather than a refusal.
                decisions.append(
                    WindowDecision(
                        offset_seconds=window.offset_seconds,
                        skip=SkipReason.LOWER_PRIORITY,
                        detail="a higher-priority window already produced the intent",
                    )
                )
                self.intents_skipped += 1
                continue

            decision = self.decide_window(market, window, health, now)
            decisions.append(decision)
            if decision.acted:
                already_acted = True

        return DecisionOutcome(market_slug=market.slug, decisions=tuple(decisions))

    # ── one window ───────────────────────────────────────────────────────────

    def decide_window(
        self,
        market: MarketInstance,
        window: ExecutionWindow,
        health: RuntimeHealth,
        now: float,
    ) -> WindowDecision:
        """The full six-step pipeline for one fired window."""
        offset = window.offset_seconds

        # 2. Validate. One read of the frozen state; nothing below reads it again.
        snapshot = snapshot_for(market, window)
        if snapshot is None:
            self.intents_skipped += 1
            return WindowDecision(
                offset_seconds=offset,
                skip=SkipReason.INCOMPLETE,
                detail="frozen state is incomplete; nothing to act on",
            )

        # The quote is fetched before the gates because gates 10 and 11 evaluate the
        # price and size the strategy derives from it. Fetching it after the cheap
        # gates would mean a market that is not tradeable at all still costs a book
        # read; fetching it before them would be the same cost on every pass. The
        # cheap gates therefore run first, against a quote of zero only when there
        # is genuinely no quote.
        quote = self._quote(snapshot.market_slug, snapshot.direction)
        if quote is None or quote <= _ZERO:
            self.intents_skipped += 1
            return WindowDecision(
                offset_seconds=offset,
                skip=SkipReason.NO_QUOTE,
                detail=f"no usable {snapshot.direction.value} quote",
            )

        strategy = self._registry.default
        description = strategy.describe()
        proposal = strategy.decide(
            context_for(snapshot, self._strategy_config, quote_price=quote)
        )

        # 3. Risk gates. Evaluated against the strategy's PROPOSED price and size,
        #    which is why the strategy is consulted first — a proposal is not an
        #    authorisation, and the gates are what turn one into the other.
        #
        #    The duration of this call is measured, but not here: A0 forbids this
        #    layer a clock of any kind. The runtime injects a stopwatch-wrapped
        #    engine, so the measurement lives where time is already allowed.
        verdict = self._risk.evaluate(
            self._risk_context(market, snapshot, health, proposal.limit_price, proposal.size)
        )
        if verdict.denied and verdict.reason is not None:
            self.intents_denied += 1
            log_event(
                logging.WARNING,
                "Intent Denied",
                f"{verdict.gate_id} {verdict.gate}  {verdict.reason.value}  "
                f"{snapshot.market_slug}  {offset}s  {health.mode or 'UNKNOWN'}  "
                f"{verdict.detail}",
                logger=self._logger,
            )

            return WindowDecision(
                offset_seconds=offset,
                denial=verdict.reason,
                gate=verdict.gate,
                detail=verdict.detail,
            )

        # The strategy declining is checked AFTER the gates so that a trade refused
        # by policy reports the policy, not the sizing. A denial the operator can
        # act on beats a sizing note they cannot.
        if not proposal.act:
            self.intents_skipped += 1
            return WindowDecision(
                offset_seconds=offset,
                skip=SkipReason.STRATEGY_HELD,
                detail=proposal.reason,
            )

        # 4. Create.
        intent = build_intent(
            snapshot, proposal, strategy_id=description.strategy_id, created_at=now
        )

        # 5. Persist. SQLite arbitrates. A False return means another path already
        #    recorded an intent for this window — between the duplicate gate above
        #    and this insert — so the correct outcome is DUPLICATE_INTENT rather than
        #    a second in-memory intent that would submit a second order.
        if not self._store.save_intent(intent):
            self.intents_denied += 1
            return WindowDecision(
                offset_seconds=offset,
                denial=DenialReason.DUPLICATE_INTENT,
                gate="duplicate_intent",
                detail="the UNIQUE constraint refused a second intent for this window",
            )

        # Reserve only after the row is on disk. Reserving first and failing the
        # insert would consume a quota slot for a trade that does not exist, and
        # nothing would ever release it (hazard H2).
        self._quota.reserve(market, offset)
        market.intents.append(intent)
        self.intents_created += 1
        log_event(
            logging.INFO,
            "Intent Created",
            f"{snapshot.market_slug} {offset}s  {snapshot.direction.value}  "
            f"{dec_str(intent.size)} @ {dec_str(intent.limit_price)}  "
            f"twap {dec_str(snapshot.signal_twap)} vs trigger "
            f"{dec_str(snapshot.locked_trigger)}",
            logger=self._logger,
        )

        # 6. Return.
        return WindowDecision(offset_seconds=offset, intent=intent)

    # ── gate inputs ──────────────────────────────────────────────────────────

    def _risk_context(
        self,
        market: MarketInstance,
        snapshot: DecisionSnapshot,
        health: RuntimeHealth,
        limit_price: Decimal,
        size: Decimal,
    ) -> RiskContext:
        """Gather every gate's input, once.

        `window_triggered` is taken from the persisted FIRED state rather than by
        re-running `is_triggered` against the current TWAP. Re-testing would compare
        the frozen trigger against a TWAP that has moved since the window fired, and
        a window that legitimately fired could then be refused as un-triggered.
        """
        quota = self._quota.snapshot(market)
        return RiskContext(
            trading_enabled=health.trading_enabled,
            spec_status=health.spec_status,
            execution_armed=health.execution_armed,
            paused=health.paused,
            trading_disabled_reason=health.trading_disabled_reason,
            phase=market.phase,
            window_triggered=snapshot.state is WindowState.FIRED,
            ptb=snapshot.ptb,
            strategy_enabled=len(self._registry) > 0,
            strategy_id=self._registry.default.describe().strategy_id,
            intent_exists=self._store.has_intent(
                snapshot.market_slug, snapshot.offset_seconds
            ),
            quota_used=quota.used,
            quota_reserved=quota.reserved,
            max_trades_per_market=quota.limit,
            direction=snapshot.direction,
            directions_held=market.directions_held(),
            allow_opposing_directions=self._limits.allow_opposing_directions,
            open_positions=health.open_positions,
            max_concurrent_positions=self._limits.max_concurrent_positions,
            limit_price=limit_price,
            size=size,
            entry_price_min=self._limits.entry_price_min,
            entry_price_max=self._limits.entry_price_max,
            min_tradable_size=self._limits.min_tradable_size,
            daily_loss_usd=health.daily_loss_usd,
            consecutive_losses=health.consecutive_losses,
            max_daily_loss_usd=self._limits.max_daily_loss_usd,
            max_consecutive_losses=self._limits.max_consecutive_losses,
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
            trace_id=trace_id_for(snapshot.market_slug, snapshot.offset_seconds),
        )
