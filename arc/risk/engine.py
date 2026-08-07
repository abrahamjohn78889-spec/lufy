"""The Risk Engine: the union of every gate, in one deterministic order.

Nineteen gates. Each owns exactly one DenialReason, so a rejection line names the
condition that refused rather than a category that several conditions share. The
order is fixed and the evaluation short-circuits on the first denial, which is what
makes the rejection log stable: the same situation always reports the same reason,
so a change in the log means a change in the world rather than a change in
evaluation order.

Why this order. The cheapest and most global gates run first — trading disabled,
not armed, wrong phase, window not triggered — because none of them need a quote, a
book, a size or a database read, and a market that is not tradeable at all should
not cause any of that work. The gates that need the strategy's output (price, size)
run last.

Gates 16 to 19 are the LIVE-MONEY preconditions: the supervisor is ready, the
wallet is connected, reconciliation left nothing unaccounted for, and the account
can actually pay for the order. They live here, and not in the execution adapter,
because this is the single admission point before any submission — a check the
adapter owned would be a second decision layer that V1 never exercises, so the
paper run would stop being evidence about the live one.

This engine is PURE. It evaluates a frozen RiskContext and returns a verdict. It
opens no socket, holds no wallet, submits nothing and mutates nothing. The A8
submission boundary is enforced by gate 1 refusing every intent while the
settlement spec is unverified — enforced HERE because this is the single place an
order can be authorised, so a caller who forgets to consult the runtime flag still
cannot submit. Gate 2 is the operator's separate arming switch: BOTH must pass
before an intent may become an order, and neither can override the other.

There is no lead-time gate. The lead-time gate is repealed entirely (A10/D1): the
only execution boundary is the market phase, and nothing here reads a clock to
decide whether an action is "too late".
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Final

from arc.domain.enums import DenialReason, Direction, MarketPhase, SettlementSpecStatus
from arc.domain.money import dec_str

__all__ = ["GATE_ORDER", "RiskContext", "RiskEngine", "RiskVerdict"]

_ZERO: Final[Decimal] = Decimal("0")

# The gate names in evaluation order. Exported so the test suite can assert the
# order is what it claims to be rather than trusting the source reading top to
# bottom, and so a reordering is a visible diff in one place.
GATE_ORDER: Final[tuple[str, ...]] = (
    "trading_enabled",
    "execution_armed",
    "market_phase",
    "window_triggered",
    "price_to_beat",
    "strategy_enabled",
    "duplicate_intent",
    "trade_quota",
    "opposing_direction",
    "position_limit",
    "entry_band",
    "exchange_minimum",
    "loss_limits",
    "feed_freshness",
    "runtime_health",
    "supervisor_ready",
    "wallet_connected",
    "orphan_orders",
    "available_balance",
)


@dataclass(frozen=True, slots=True)
class RiskContext:
    """Everything the gates evaluate. Frozen, and gathered by the caller.

    Frozen because a gate that could reach live state would evaluate a different
    world than the one the earlier gates saw, and the verdict would then depend on
    how long evaluation took. Every value is read once, by the Decision Engine,
    before the first gate runs.
    """

    # ── gate 1: trading enabled ──────────────────────────────────────────────
    trading_enabled: bool
    spec_status: SettlementSpecStatus
    paused: bool = False
    trading_disabled_reason: str = ""

    # ── gate 2: operator arming ──────────────────────────────────────────────
    # Defaults False so a caller that forgets to gather it submits nothing rather
    # than submitting on an assumption. This is the operator's own switch and is
    # never inferred from runtime health.
    execution_armed: bool = False

    # ── gate 3: market phase ─────────────────────────────────────────────────
    phase: MarketPhase = MarketPhase.ACTIVE

    # ── gate 4: window triggered ─────────────────────────────────────────────
    window_triggered: bool = False

    # ── gate 5: official PTB ─────────────────────────────────────────────────
    ptb: Decimal | None = None

    # ── gate 6: strategy enabled ─────────────────────────────────────────────
    strategy_enabled: bool = True
    strategy_id: str = ""

    # ── gate 7: duplicate intent ─────────────────────────────────────────────
    intent_exists: bool = False

    # ── gate 8: trade quota ──────────────────────────────────────────────────
    quota_used: int = 0
    quota_reserved: int = 0
    max_trades_per_market: int = 0

    # ── gate 9: opposing direction ───────────────────────────────────────────
    direction: Direction = Direction.UP
    directions_held: frozenset[Direction] = field(default_factory=frozenset)
    allow_opposing_directions: bool = False

    # ── gate 10: concurrent positions ─────────────────────────────────────────
    open_positions: int = 0
    max_concurrent_positions: int = 0

    # ── gates 11 and 12: price and size ──────────────────────────────────────
    limit_price: Decimal = _ZERO
    size: Decimal = _ZERO
    entry_price_min: Decimal = _ZERO
    entry_price_max: Decimal = _ZERO
    min_tradable_size: Decimal = _ZERO

    # ── gate 13: loss limits ─────────────────────────────────────────────────
    daily_loss_usd: Decimal = _ZERO
    consecutive_losses: int = 0
    max_daily_loss_usd: Decimal = _ZERO
    max_consecutive_losses: int = 0

    # ── gate 14: feed freshness ──────────────────────────────────────────────
    feed_blocked: bool = False
    feed_age_ms: float | None = None

    # ── gate 15: clock drift and runtime health ──────────────────────────────
    clock_drift_critical: bool = False
    clock_drift_ms: float = 0.0
    runtime_healthy: bool = True
    runtime_detail: str = ""

    # ── gate 16: supervisor readiness ────────────────────────────────────────
    # Whether the supervisor still considers this runtime the attached one. The
    # runtime cannot see its own detachment — `runtime_healthy` above is the
    # runtime reporting on itself, and a cancelled-but-still-looping object
    # reports itself perfectly healthy while nothing owns it any more.
    supervisor_ready: bool = True
    supervisor_detail: str = ""

    # ── gate 17: wallet connectivity ─────────────────────────────────────────
    # False only on a wallet the venue refused to answer for. V1 has no venue
    # account at all, which is not a disconnection: the paper wallet reports
    # PAPER and passes, or V1 could never produce the validation evidence V2
    # depends on.
    wallet_connected: bool = True
    wallet_status: str = ""

    # ── gate 18: orphan orders ───────────────────────────────────────────────
    # Orders at the venue that reconciliation could not account for. Named
    # individually rather than counted, so the denial line says which ones.
    orphan_orders: tuple[str, ...] = ()

    # ── gate 19: available balance ───────────────────────────────────────────
    # None means no official source reported one, and that is NOT a denial: the
    # venue publishes collateral only for a live account, and refusing on an
    # absent figure would be refusing on a number ARC invented. A DISCONNECTED
    # wallet is caught by gate 17 above, which is the case this would otherwise
    # be standing in for. The cost is not carried separately — it is
    # limit_price * size, both already frozen into this context above, and a
    # second copy could disagree with the order actually submitted.
    available_balance: Decimal | None = None


@dataclass(frozen=True, slots=True)
class RiskVerdict:
    """The outcome. `gate` names which gate spoke, whether it allowed or denied.

    Carrying the gate name as well as the reason makes a denial traceable to one
    line of this module. Two gates can never produce the same reason, but a reason
    alone does not say which of several conditions inside a gate fired, and the
    detail string is prose.
    """

    allowed: bool
    gate: str = ""
    reason: DenialReason | None = None
    detail: str = ""

    @property
    def denied(self) -> bool:
        return not self.allowed


_ALLOWED: Final[RiskVerdict] = RiskVerdict(allowed=True)


class RiskEngine:
    """Evaluates the gates. Stateless.

    __slots__ = () so the engine cannot accumulate per-market state: a risk engine
    that remembered the previous market's quota would deny trades on a fresh
    market and the denial would look like a correctly-working limit (A11).
    """

    __slots__ = ()

    def evaluate(self, context: RiskContext) -> RiskVerdict:
        """Run every gate in GATE_ORDER, stopping at the first denial."""
        for name in GATE_ORDER:
            # Looked up by name from GATE_ORDER rather than written as a literal
            # chain of calls, so the declared order and the executed order cannot
            # drift apart: a gate renamed without updating GATE_ORDER raises
            # AttributeError on the first evaluation instead of silently never
            # running.
            gate: Callable[[RiskContext], RiskVerdict] = getattr(self, f"_gate_{name}")
            verdict = gate(context)
            if verdict.denied:
                return verdict
        return _ALLOWED

    # ── 1 ────────────────────────────────────────────────────────────────────

    def _gate_trading_enabled(self, c: RiskContext) -> RiskVerdict:
        """The A8 boundary. Unverified spec means record decisions, submit none.

        Placed first and enforced here because this is the only place an order can
        be authorised. The process still boots, the dashboard still works, feeds
        still run and windows still freeze and evaluate — it simply never submits,
        so waiting produces the dataset that answers U1..U4 instead of nothing.
        """
        if c.spec_status is not SettlementSpecStatus.VERIFIED:
            return RiskVerdict(
                allowed=False,
                gate="trading_enabled",
                reason=DenialReason.TRADING_DISABLED_SPEC_UNVERIFIED,
                detail=f"settlement spec is {c.spec_status.value}",
            )
        if c.paused:
            return RiskVerdict(
                allowed=False,
                gate="trading_enabled",
                reason=DenialReason.TRADING_PAUSED,
                detail="operator paused trading",
            )
        if not c.trading_enabled:
            return RiskVerdict(
                allowed=False,
                gate="trading_enabled",
                reason=DenialReason.TRADING_DISABLED_SPEC_UNVERIFIED,
                detail=c.trading_disabled_reason or "trading is disabled",
            )
        return _ALLOWED

    # ── 2 ────────────────────────────────────────────────────────────────────

    def _gate_execution_armed(self, c: RiskContext) -> RiskVerdict:
        """The OPERATOR boundary, independent of gate 1's SYSTEM boundary.

        Two gates rather than one flag because they answer different questions and
        have different owners. Gate 1 asks whether ARC is technically permitted to
        trade and is moved only by ARC's own safety checks; this gate asks whether
        the operator has asked it to, and is moved only by the Start/Stop Trading
        control. Collapsing them would let a dashboard click clear a disable that a
        failed spec verification imposed — the operator would be able to override
        system safety by pressing a button, which is exactly backwards.

        Ordered SECOND, after the system gate, so that when both are blocking, the
        reported reason is the system one. An operator told "not armed" while the
        real obstacle is an unverified spec would arm the bot and see nothing
        happen, with no reason given for the second refusal.
        """
        if not c.execution_armed:
            return RiskVerdict(
                allowed=False,
                gate="execution_armed",
                reason=DenialReason.EXECUTION_NOT_ARMED,
                detail="the Limit Order Engine is waiting for the operator to start trading",
            )
        return _ALLOWED

    # ── 3 ────────────────────────────────────────────────────────────────────

    def _gate_market_phase(self, c: RiskContext) -> RiskVerdict:
        """Phase is the ONLY execution boundary that exists (A10/D1).

        CANCELLING has its own reason because it is the ordinary end-of-market
        outcome and must be distinguishable in the log from a market that was
        never tradeable. DEAD means no official PTB was obtainable, so the market
        is never traded at all (A1 Rule 1).
        """
        if c.phase is MarketPhase.CANCELLING:
            return RiskVerdict(
                allowed=False,
                gate="market_phase",
                reason=DenialReason.MARKET_CANCELLING,
                detail="the cancellation sweep has begun",
            )
        if c.phase is MarketPhase.DEAD:
            return RiskVerdict(
                allowed=False,
                gate="market_phase",
                reason=DenialReason.MARKET_DEAD,
                detail="no official PTB was obtainable for this market",
            )
        if c.phase is not MarketPhase.ACTIVE:
            return RiskVerdict(
                allowed=False,
                gate="market_phase",
                reason=DenialReason.MARKET_NOT_ACTIVE,
                detail=f"phase is {c.phase.value}",
            )
        return _ALLOWED

    # ── 4 ────────────────────────────────────────────────────────────────────

    def _gate_window_triggered(self, c: RiskContext) -> RiskVerdict:
        """Only a window whose frozen trigger was satisfied may become an intent.

        Defence in depth against a caller that reaches the Decision Engine with a
        window the Window Engine never fired — which would trade on no signal at
        all while every log line still read normally.
        """
        if not c.window_triggered:
            return RiskVerdict(
                allowed=False,
                gate="window_triggered",
                reason=DenialReason.WINDOW_NOT_TRIGGERED,
                detail="the frozen trigger has not been satisfied",
            )
        return _ALLOWED

    # ── 5 ────────────────────────────────────────────────────────────────────

    def _gate_price_to_beat(self, c: RiskContext) -> RiskVerdict:
        """No official PTB, no trade. Never calculated, never estimated (A1 Rule 1).

        Separate from the DEAD phase check: a market can be ACTIVE with the PTB
        freeze still absent, and trading it would mean trading against a reference
        this process invented.
        """
        if c.ptb is None or c.ptb <= _ZERO:
            return RiskVerdict(
                allowed=False,
                gate="price_to_beat",
                reason=DenialReason.PTB_UNAVAILABLE,
                detail="no official Price To Beat is frozen on this market",
            )
        return _ALLOWED

    # ── 6 ────────────────────────────────────────────────────────────────────

    def _gate_strategy_enabled(self, c: RiskContext) -> RiskVerdict:
        """A disabled or missing strategy must say so, not silently not trade.

        Without this gate a registry that lost its strategy would produce five
        ordinary non-signals per market and nothing anywhere would report why.
        """
        if not c.strategy_enabled:
            return RiskVerdict(
                allowed=False,
                gate="strategy_enabled",
                reason=DenialReason.STRATEGY_DISABLED,
                detail=f"strategy {c.strategy_id or '(none)'} is not enabled",
            )
        return _ALLOWED

    # ── 7 ────────────────────────────────────────────────────────────────────

    def _gate_duplicate_intent(self, c: RiskContext) -> RiskVerdict:
        """Exactly one intent per window, ever (A12).

        This is the cheap in-memory check. The authority is the SQLite UNIQUE
        constraint on (market_slug, offset_seconds), which is what holds across a
        crash between the decision and the submission — this gate exists so the
        ordinary case is refused with a named reason rather than by an insert
        that returns no row.
        """
        if c.intent_exists:
            return RiskVerdict(
                allowed=False,
                gate="duplicate_intent",
                reason=DenialReason.DUPLICATE_INTENT,
                detail="this window already has an intent",
            )
        return _ALLOWED

    # ── 8 ────────────────────────────────────────────────────────────────────

    def _gate_trade_quota(self, c: RiskContext) -> RiskVerdict:
        """Used plus RESERVED against the per-market budget (hazard H2).

        Reservations are counted because admission and fill are not simultaneous.
        Three windows can each pass a used-only check within the same second,
        before any of them fills, and open four positions against a three-trade
        budget. Counting reservations closes that window.
        """
        committed = c.quota_used + c.quota_reserved
        if committed >= c.max_trades_per_market:
            return RiskVerdict(
                allowed=False,
                gate="trade_quota",
                reason=DenialReason.TRADE_QUOTA_EXHAUSTED,
                detail=(
                    f"{c.quota_used} used + {c.quota_reserved} reserved of "
                    f"{c.max_trades_per_market}"
                ),
            )
        return _ALLOWED

    # ── 9 ────────────────────────────────────────────────────────────────────

    def _gate_opposing_direction(self, c: RiskContext) -> RiskVerdict:
        """Holding both sides of one market is a guaranteed loss (hazard H3).

        UP at 0.79 plus DOWN at 0.22 costs 1.01 and returns exactly 1.00. It is
        not a hedge; it is a fee. Blocked by default, and the operator may enable
        it deliberately — which raises an advisory warning at config time.
        """
        if c.allow_opposing_directions:
            return _ALLOWED
        if c.direction.opposite in c.directions_held:
            return RiskVerdict(
                allowed=False,
                gate="opposing_direction",
                reason=DenialReason.OPPOSING_DIRECTION_BLOCKED,
                detail=(
                    f"{c.direction.opposite.value} is already held; "
                    f"adding {c.direction.value} costs more than it can return"
                ),
            )
        return _ALLOWED

    # ── 10 ───────────────────────────────────────────────────────────────────

    def _gate_position_limit(self, c: RiskContext) -> RiskVerdict:
        """Concurrent open positions across the process, not per market.

        Two markets are live at every boundary (D6), so a per-market quota alone
        permits twice the intended exposure for five seconds out of every three
        hundred — the exact seconds the late windows trade in.
        """
        if c.open_positions >= c.max_concurrent_positions:
            return RiskVerdict(
                allowed=False,
                gate="position_limit",
                reason=DenialReason.POSITION_LIMIT_REACHED,
                detail=f"{c.open_positions} open of {c.max_concurrent_positions}",
            )
        return _ALLOWED

    # ── 11 ───────────────────────────────────────────────────────────────────

    def _gate_entry_band(self, c: RiskContext) -> RiskVerdict:
        """The limit price must sit inside the configured band.

        The price arriving here is ALREADY floored to the tick (defect D2). That
        ordering matters: 0.857 floors to 0.85 and is admissible, while validating
        first and flooring second would reject a price the venue would have
        accepted — and, in the other direction, admit one it would not.

        Above and below the band have separate reasons because they mean opposite
        things: too expensive is a trade not worth taking, too cheap is a signal
        the book disagrees with.
        """
        if c.limit_price > c.entry_price_max:
            return RiskVerdict(
                allowed=False,
                gate="entry_band",
                reason=DenialReason.ENTRY_PRICE_LIMIT,
                detail=f"{dec_str(c.limit_price)} > {dec_str(c.entry_price_max)}",
            )
        if c.limit_price < c.entry_price_min:
            return RiskVerdict(
                allowed=False,
                gate="entry_band",
                reason=DenialReason.ENTRY_PRICE_BELOW_MIN,
                detail=f"{dec_str(c.limit_price)} < {dec_str(c.entry_price_min)}",
            )
        return _ALLOWED

    # ── 12 ───────────────────────────────────────────────────────────────────

    def _gate_exchange_minimum(self, c: RiskContext) -> RiskVerdict:
        """Size must reach the venue's minimum tradable quantity.

        A sub-minimum order is rejected by the venue, and a rejection consumes a
        reservation and a round trip in the last seconds of a market — the moment
        with the least time to recover from either.
        """
        if c.size < c.min_tradable_size or c.size <= _ZERO:
            return RiskVerdict(
                allowed=False,
                gate="exchange_minimum",
                reason=DenialReason.SIZE_BELOW_EXCHANGE_MINIMUM,
                detail=f"{dec_str(c.size)} < {dec_str(c.min_tradable_size)}",
            )
        return _ALLOWED

    # ── 13 ───────────────────────────────────────────────────────────────────

    def _gate_loss_limits(self, c: RiskContext) -> RiskVerdict:
        """Daily loss and consecutive losses. Zero on either DISABLES that limit.

        Both share one reason because both mean the same thing to the operator —
        stop trading today — and the detail says which threshold was reached. A
        losing streak and a large single loss are separate thresholds because they
        are separate signals; collapsing them into one number hides whichever
        would have fired second.

        `daily_loss_usd` is a POSITIVE magnitude of loss, so a profitable day
        arrives here as zero rather than as a negative number that would compare
        as under every threshold by accident.
        """
        if c.max_daily_loss_usd > _ZERO and c.daily_loss_usd >= c.max_daily_loss_usd:
            return RiskVerdict(
                allowed=False,
                gate="loss_limits",
                reason=DenialReason.LOSS_LIMIT_REACHED,
                detail=(
                    f"daily loss {dec_str(c.daily_loss_usd)} reached "
                    f"{dec_str(c.max_daily_loss_usd)}"
                ),
            )
        if c.max_consecutive_losses > 0 and c.consecutive_losses >= c.max_consecutive_losses:
            return RiskVerdict(
                allowed=False,
                gate="loss_limits",
                reason=DenialReason.LOSS_LIMIT_REACHED,
                detail=(
                    f"{c.consecutive_losses} consecutive losses reached "
                    f"{c.max_consecutive_losses}"
                ),
            )
        return _ALLOWED

    # ── 14 ───────────────────────────────────────────────────────────────────

    def _gate_feed_freshness(self, c: RiskContext) -> RiskVerdict:
        """A stale feed means the signal TWAP is stale.

        The failure this prevents is the quiet one: the socket is up, the process
        is healthy, the last observation is forty seconds old, and the TWAP the
        window compares against describes a market that has since moved. Nothing
        raises. So freshness is a gate rather than a warning.
        """
        if c.feed_blocked:
            age = "never" if c.feed_age_ms is None else f"{c.feed_age_ms:.0f}ms"
            return RiskVerdict(
                allowed=False,
                gate="feed_freshness",
                reason=DenialReason.FEED_STALE,
                detail=f"last observation {age} ago",
            )
        return _ALLOWED

    # ── 15 ───────────────────────────────────────────────────────────────────

    def _gate_runtime_health(self, c: RiskContext) -> RiskVerdict:
        """Critical clock drift, then general runtime health.

        Drift is checked on the ABSOLUTE offset: -900ms is as bad as +900ms. On a
        three-second window, drift near a one-second threshold consumes a third of
        the window — the process would compute a correct window_ts for the wrong
        market and freeze against the wrong market's opening TWAP.

        This is last because it is the broadest and the least specific: reaching it
        means every trade-specific condition already passed, so a denial here is
        genuinely about the machine rather than about this trade.
        """
        if c.clock_drift_critical:
            return RiskVerdict(
                allowed=False,
                gate="runtime_health",
                reason=DenialReason.CLOCK_DRIFT_CRITICAL,
                detail=f"clock offset {c.clock_drift_ms:.0f}ms exceeds the critical threshold",
            )
        if not c.runtime_healthy:
            return RiskVerdict(
                allowed=False,
                gate="runtime_health",
                reason=DenialReason.RUNTIME_UNHEALTHY,
                detail=c.runtime_detail or "the runtime reported itself unhealthy",
            )
        return _ALLOWED

    # ── 16 ───────────────────────────────────────────────────────────────────

    def _gate_supervisor_ready(self, c: RiskContext) -> RiskVerdict:
        """The supervisor's view of the run, which the runtime cannot see itself.

        Gate 15 is the runtime reporting on its own status. This is the layer above
        reporting on whether that runtime is still the attached one and whether its
        task is alive. The failure it catches is a runtime whose task was cancelled
        or replaced mid-teardown but whose loop reaches one more decision pass: it
        reports itself RUNNING, every engine looks correct, and the order it submits
        belongs to a run nobody owns any more.
        """
        if not c.supervisor_ready:
            return RiskVerdict(
                allowed=False,
                gate="supervisor_ready",
                reason=DenialReason.RUNTIME_SUPERVISOR_NOT_READY,
                detail=c.supervisor_detail or "the runtime supervisor is not READY",
            )
        return _ALLOWED

    # ── 17 ───────────────────────────────────────────────────────────────────

    def _gate_wallet_connected(self, c: RiskContext) -> RiskVerdict:
        """A wallet the venue would not answer for makes every balance stale.

        The quiet failure: the deck still shows the last balance that was true, the
        gates below still compare against it, and the operator reads a screen that
        describes an account state from some minutes ago. Refused rather than
        warned, because the balance gate underneath would otherwise pass on a
        remembered number.

        V1 has no venue account and is NOT disconnected — `wallet_connected`
        arrives True for the paper wallet. A paper run refused here would produce
        no validation evidence at all, which is the one thing V2 depends on.
        """
        if not c.wallet_connected:
            return RiskVerdict(
                allowed=False,
                gate="wallet_connected",
                reason=DenialReason.WALLET_DISCONNECTED,
                detail=c.wallet_status or "the venue account could not be read",
            )
        return _ALLOWED

    # ── 18 ───────────────────────────────────────────────────────────────────

    def _gate_orphan_orders(self, c: RiskContext) -> RiskVerdict:
        """An order at the venue that reconciliation could not account for (A14).

        Submitting on top of one doubles the position while both orders look
        entirely genuine, and neither the ledger nor the deck would show anything
        wrong until settlement. The orphans are named in the detail rather than
        counted: "2 orphans" sends the operator to the logs, which is the trip this
        gate exists to remove.
        """
        if c.orphan_orders:
            return RiskVerdict(
                allowed=False,
                gate="orphan_orders",
                reason=DenialReason.ORPHAN_ORDERS_UNRECONCILED,
                detail=f"unreconciled at the venue: {', '.join(c.orphan_orders)}",
            )
        return _ALLOWED

    # ── 19 ───────────────────────────────────────────────────────────────────

    def _gate_available_balance(self, c: RiskContext) -> RiskVerdict:
        """The account must be able to pay for this order.

        Cost is `limit_price * size` — the same two frozen values gate 11 and gate
        12 already checked, so what is priced here is what is submitted.

        Last, deliberately. It is the only gate that needs both the strategy's
        output and a venue read, and reaching it means every cheaper condition
        already passed, so "not enough money" is the true answer rather than the
        first of several.

        `available_balance is None` ALLOWS. None means no official source reported
        a figure, which is V1's normal state and, per the wallet contract, the
        state of any field the venue does not publish. Denying on an absent number
        would be denying on a number ARC made up; a wallet that genuinely failed to
        read is DISCONNECTED and gate 17 already refused it.
        """
        if c.available_balance is None:
            return _ALLOWED
        cost = c.limit_price * c.size
        if cost > c.available_balance:
            return RiskVerdict(
                allowed=False,
                gate="available_balance",
                reason=DenialReason.INSUFFICIENT_BALANCE,
                detail=(
                    f"order costs {dec_str(cost)} and "
                    f"{dec_str(c.available_balance)} is available"
                ),
            )
        return _ALLOWED
