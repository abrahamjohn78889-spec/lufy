"""Domain models.

The central idea (A11): all per-market mutable state lives on a MarketInstance
object that is thrown away at close and never reused. There is no reset(), no
clear(), no reuse path — because the dominant bug class in a five-minute bot is
stale state bleeding across a market boundary, and an object you throw away
cannot bleed.

Three quantities that are never conflated, and never named a generic `twap` (A6):

    signal_twap      ARC's own 300s cumulative mean. The STRATEGY INPUT.
    settlement_twap  The VENUE's 30s Chainlink mean. THE OUTCOME QUANTITY.
                     Recorded observationally; feeds no decision in any phase.
    ptb              The official opening reference. Never calculated.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Final

from arc.domain.enums import (
    LIVE_ORDER_STATES,
    Direction,
    MarketPhase,
    OrderState,
    Outcome,
    WindowState,
)
from arc.domain.money import dec_str, to_decimal
from arc.domain.timing import close_ts_for, slug_for, windows_by_priority
from arc.errors import (
    NoDirectionError,
    ObservationRejectedError,
    WindowFreezeError,
)

__all__ = [
    "ExecutionIntent",
    "ExecutionWindow",
    "Fill",
    "MarketInstance",
    "Observation",
    "Order",
    "Settlement",
    "TwapAccumulator",
]

_ZERO: Final[Decimal] = Decimal("0")

# Phases in which a market still accepts observations.
#
# SETTLING is included deliberately: the market has closed but the venue's
# resolution has not arrived, and the observations arriving in that gap are
# exactly the ones inside the settlement averaging window. Refusing them would
# discard the data needed to record settlement_twap at all.
#
# DEAD and SETTLED refuse: a DEAD market has no official PTB and will never be
# traded, and a SETTLED market's signal TWAP is a closed historical record that
# must not move after the fact.
_OBSERVING_PHASES: Final[frozenset[MarketPhase]] = frozenset(
    {
        MarketPhase.DISCOVERED,
        MarketPhase.ACTIVE,
        MarketPhase.CANCELLING,
        MarketPhase.SETTLING,
    }
)


@dataclass(frozen=True, slots=True)
class Observation:
    """One Chainlink price observation from the official feed.

    Frozen: an observation already folded into a running sum must not be mutated
    afterwards, or the sum and the observations that produced it disagree with no
    way to detect which is right.
    """

    ts: float
    price: Decimal
    feed_id: str = ""
    window_seconds: int | None = None

    def __post_init__(self) -> None:
        price = to_decimal(self.price)
        if price <= _ZERO:
            raise ValueError(f"observation price must be positive, got {price}")
        object.__setattr__(self, "price", price)


@dataclass(slots=True)
class TwapAccumulator:
    """Exact-sum cumulative mean. ARC's signal TWAP for ONE market.

    Stores running_sum and observation_count; the mean is computed on READ.

    The incremental form M += (x - M) / n is forbidden (hazard H1). It divides at
    every step, so every step rounds, and across ~300 observations the error
    accumulates monotonically — silently moving the locked trigger away from the
    value the operator configured, with nothing reporting it. Summing exactly and
    dividing once means exactly one rounding, at the point of use.

    There is no reset() and no clear(). "TWAP resets per market" is satisfied by
    construction: a new market is a new object that starts at zero, so there is no
    reset path that can be forgotten in one of the places that needed it.
    """

    running_sum: Decimal = _ZERO
    observation_count: int = 0

    def add(self, price: Decimal | int | str) -> None:
        """Fold one observation into the exact sum."""
        self.running_sum += to_decimal(price)
        self.observation_count += 1

    @property
    def mean(self) -> Decimal | None:
        """Signal TWAP, computed on read. None when empty.

        None rather than zero: a market that has received no observations yet and
        a market whose average is genuinely zero must not display identically, and
        a zero here would be compared against a real PTB and produce a confident
        DOWN direction from no data at all.
        """
        if self.observation_count == 0:
            return None
        return self.running_sum / self.observation_count

    @classmethod
    def restore(cls, running_sum: Decimal | int | str, observation_count: int) -> TwapAccumulator:
        """Rebuild from persisted state.

        Takes the sum and the count, not the mean. Restoring from a stored mean
        would bake in one rounding and then keep accumulating on top of it, so the
        restored TWAP would drift away from the uninterrupted one.
        """
        if observation_count < 0:
            raise ValueError(f"observation_count must not be negative, got {observation_count}")
        return cls(running_sum=to_decimal(running_sum), observation_count=observation_count)


@dataclass(slots=True)
class ExecutionWindow:
    """One execution window: 15s, 10s, 7s, 5s or 3s before close.

    Five values freeze together, atomically, and are immutable thereafter —
    including across a restart, where they are reloaded verbatim rather than
    recomputed (A4).
    """

    offset_seconds: int
    state: WindowState = WindowState.PENDING
    opening_twap: Decimal | None = None
    ptb: Decimal | None = None
    buffer: Decimal | None = None
    direction: Direction | None = None
    locked_trigger: Decimal | None = None
    frozen_at: float | None = None
    fired_at: float | None = None

    @property
    def is_frozen(self) -> bool:
        return self.state in (WindowState.FROZEN, WindowState.FIRED)

    def freeze(
        self,
        *,
        opening_twap: Decimal,
        ptb: Decimal,
        buffer: Decimal,
        frozen_at: float,
    ) -> None:
        """Lock all five values atomically. All-or-nothing.

        Every input is validated and the derived values are computed BEFORE any
        field is assigned. A window left half-frozen would hold a real
        opening_twap beside a defaulted buffer, producing a locked trigger that
        was never configured while looking completely healthy (A12). On any
        failure the window stays PENDING with all five values still None.
        """
        if self.is_frozen:
            raise WindowFreezeError(
                f"window {self.offset_seconds}s is already frozen; frozen values are immutable"
            )

        # Compute into locals first. If any of this raises, nothing has been
        # written and the window is untouched.
        try:
            twap = to_decimal(opening_twap)
            price_to_beat = to_decimal(ptb)
            buf = to_decimal(buffer)
        except (TypeError, ValueError) as exc:
            raise WindowFreezeError(
                f"window {self.offset_seconds}s freeze rejected: {exc}"
            ) from exc

        if buf <= _ZERO:
            raise WindowFreezeError(
                f"window {self.offset_seconds}s freeze rejected: buffer must be positive, got {buf}"
            )
        if price_to_beat <= _ZERO:
            raise WindowFreezeError(
                f"window {self.offset_seconds}s freeze rejected: ptb must be positive, "
                f"got {price_to_beat}"
            )
        if twap <= _ZERO:
            raise WindowFreezeError(
                f"window {self.offset_seconds}s freeze rejected: opening_twap must be positive, "
                f"got {twap}"
            )

        # STRICT COMPARISON ONLY. `>=` and `<=` are forbidden here by the direction
        # contract, and equality is not a tie to be broken — it is the absence of a
        # direction. Resolving it to UP (as an earlier revision did) would open a
        # real position on a market where the TWAP had not moved off the reference
        # at all, in whichever direction the tie-break happened to name.
        #
        # Raised BEFORE the assignment block, so the window is left completely
        # untouched and the caller decides its terminal state. This is the one
        # rejection that must not be retried on the next pass: direction is
        # determined once, at the opening instant, and a retry would freeze against
        # a later TWAP.
        if twap == price_to_beat:
            raise NoDirectionError(
                f"window {self.offset_seconds}s: frozen TWAP {twap} equals the official "
                f"PTB {price_to_beat}; strict comparison yields no direction, so no "
                "intent and no order (direction contract)"
            )

        direction = Direction.UP if twap > price_to_beat else Direction.DOWN
        trigger = twap + buf if direction is Direction.UP else twap - buf

        # Single assignment block, reached only when everything above succeeded.
        self.opening_twap = twap
        self.ptb = price_to_beat
        self.buffer = buf
        self.direction = direction
        self.locked_trigger = trigger
        self.frozen_at = frozen_at
        self.state = WindowState.FROZEN

    def restore_frozen(
        self,
        *,
        opening_twap: Decimal,
        ptb: Decimal,
        buffer: Decimal,
        direction: Direction,
        locked_trigger: Decimal,
        frozen_at: float,
        state: WindowState = WindowState.FROZEN,
    ) -> None:
        """Reload frozen values VERBATIM after a restart.

        direction and locked_trigger are ARGUMENTS. This method cannot recompute
        them and deliberately has no access to a current TWAP with which to try.

        This is the whole reason write-before-act survives (A4): recomputing the
        trigger from the post-restart TWAP yields a different trigger than the
        window actually froze, and the bot then keeps running, looks perfectly
        healthy, and trades a strategy nobody configured. Nothing reports it.
        """
        if state not in (WindowState.FROZEN, WindowState.FIRED, WindowState.EXPIRED):
            raise WindowFreezeError(f"cannot restore window into state {state}")
        self.opening_twap = to_decimal(opening_twap)
        self.ptb = to_decimal(ptb)
        self.buffer = to_decimal(buffer)
        self.direction = direction
        self.locked_trigger = to_decimal(locked_trigger)
        self.frozen_at = frozen_at
        self.state = state

    def is_triggered(self, signal_twap: Decimal | None) -> bool:
        """Direction-dependent trigger test. The operators are NOT interchangeable.

            UP   fires when signal_twap >= locked_trigger
            DOWN fires when signal_twap <= locked_trigger

        A single shared >= would delete the strategy rather than bias it. A DOWN
        trigger sits BELOW the opening TWAP, so at the freeze instant
        twap >= trigger is ALREADY true, and every DOWN window would fire
        immediately and unconditionally (A12).

        This is NOT the direction comparison and does not share its operators.
        Direction determination is strict (> and <, equality yields no direction);
        trigger firing is inclusive, because the locked trigger IS the threshold the
        buffer defines and landing exactly on it is reaching it. Requiring an
        overshoot would silently discard every exact touch.
        """
        if not self.is_frozen or self.locked_trigger is None or self.direction is None:
            return False
        if signal_twap is None:
            return False
        if self.direction is Direction.UP:
            return signal_twap >= self.locked_trigger
        return signal_twap <= self.locked_trigger

    def mark_fired(self, fired_at: float) -> None:
        if self.state is not WindowState.FROZEN:
            raise WindowFreezeError(
                f"window {self.offset_seconds}s cannot fire from state {self.state}"
            )
        self.state = WindowState.FIRED
        self.fired_at = fired_at

    def mark_expired(self) -> None:
        """Close an unfired window. Frozen values are retained, not cleared.

        The five values stay readable after expiry so the operator can see what
        the window was waiting for and how far away it ended up.
        """
        if self.state in (WindowState.PENDING, WindowState.FROZEN):
            self.state = WindowState.EXPIRED


@dataclass(frozen=True, slots=True)
class ExecutionIntent:
    """The decision to trade one window. Exactly one per window. Immutable.

    Uniqueness is arbitrated by a SQLite UNIQUE constraint on
    (market_slug, offset_seconds) rather than by an in-memory set, so it survives
    a crash between the decision and the submission (A12).

    Frozen, and SELF-SUFFICIENT: every value execution needs to place the order is
    carried here. That is the point. If execution re-read `market.signal_twap` or
    `window.locked_trigger` at submission time it would submit against numbers
    that moved after the decision was made — the TWAP moves continuously, which is
    the whole thing the window watches for. A frozen snapshot cannot drift.

    `close_ts` is carried for the same reason: execution needs the market's own
    close instant for its records without holding a reference to the mutable
    MarketInstance, which is dropped at close (A11).
    """

    market_slug: str
    offset_seconds: int
    direction: Direction
    signal_twap: Decimal
    locked_trigger: Decimal
    created_at: float
    intent_id: str = ""
    trace_id: str = ""
    # ── the frozen snapshot execution acts on ────────────────────────────────
    opening_twap: Decimal = _ZERO
    ptb: Decimal = _ZERO
    buffer: Decimal = _ZERO
    limit_price: Decimal = _ZERO
    size: Decimal = _ZERO
    strategy_id: str = ""
    close_ts: int = 0

    def serialize(self) -> str:
        """A stable, byte-identical rendering of the whole intent.

        Determinism is asserted on this string rather than on the object, because
        `==` on two dataclasses would pass for values that print differently —
        Decimal("0.80") == Decimal("0.8") — and a venue receives the printed form,
        not the object. `dec_str` is used for every money field so the comparison
        is over exactly the text that would go on the wire.

        `intent_id` is included: two runs of the same input must produce the same
        id, which forbids deriving it from a clock reading or a counter.

        `created_at` is deliberately EXCLUDED. It is a wall-clock reading, so two
        runs of the same observation stream on different days would differ on it
        and the determinism assertion would be about the clock rather than about
        the decision. Nothing on the wire depends on it.
        """
        parts = (
            f"intent_id={self.intent_id}",
            f"trace_id={self.trace_id}",
            f"market_slug={self.market_slug}",
            f"offset_seconds={self.offset_seconds}",
            f"direction={self.direction.value}",
            f"signal_twap={dec_str(self.signal_twap)}",
            f"locked_trigger={dec_str(self.locked_trigger)}",
            f"opening_twap={dec_str(self.opening_twap)}",
            f"ptb={dec_str(self.ptb)}",
            f"buffer={dec_str(self.buffer)}",
            f"limit_price={dec_str(self.limit_price)}",
            f"size={dec_str(self.size)}",
            f"strategy_id={self.strategy_id}",
            f"close_ts={self.close_ts}",
        )
        return "|".join(parts)


@dataclass(slots=True)
class Fill:
    """One execution against an order. Identified by the venue's fill_id.

    Persisted INSERT OR IGNORE on fill_id: a websocket redelivery of the same fill
    would otherwise double-count the position and the realised P/L.
    """

    fill_id: str
    order_id: str
    market_slug: str
    size: Decimal
    price: Decimal
    ts: float
    trace_id: str = ""
    # The engine whose order this fill executed against. Carried on the fill itself
    # rather than looked up through the order every time, so a ledger row or a
    # Telegram line can name the engine without a second query that could miss.
    engine: str = "TWAP"


@dataclass(slots=True)
class Order:
    """A standing limit order and its lifecycle."""

    order_id: str
    market_slug: str
    offset_seconds: int
    direction: Direction
    price: Decimal
    size: Decimal
    state: OrderState = OrderState.PENDING
    filled_size: Decimal = _ZERO
    created_at: float = 0.0
    updated_at: float = 0.0
    venue_order_id: str = ""
    reprice_chain_id: str = ""
    rejection_reason: str = ""
    trace_id: str = ""
    # Which engine owns this order. THE ownership field for every shared execution
    # operation: an engine-scoped sweep or reconciliation filters on it, so a
    # MAJORITY pass can never retract a TWAP order and vice versa. Defaults to TWAP
    # because nothing but TWAP has ever written an order row.
    engine: str = "TWAP"

    @property
    def is_live(self) -> bool:
        """INDETERMINATE counts as LIVE.

        An unacknowledged cancel might still be resting on the book. Treating it
        as dead would let the sweep skip it and carry an unhedged position into
        settlement (A13).
        """
        return self.state in LIVE_ORDER_STATES

    @property
    def remaining_size(self) -> Decimal:
        remaining = self.size - self.filled_size
        return remaining if remaining > _ZERO else _ZERO


@dataclass(slots=True)
class Settlement:
    """The venue's resolution of a market.

    outcome comes from the venue resolution event and is NEVER inferred from
    ARC's own signal TWAP. settlement_twap is recorded alongside it purely
    observationally. If ARC's final signal_twap implies a different outcome, the
    venue wins and the divergence is logged (A12).
    """

    market_slug: str
    outcome: Outcome
    settlement_twap: Decimal | None
    ptb: Decimal | None
    settled_at: float
    pnl: Decimal = _ZERO
    divergence_logged: bool = False
    trace_id: str = ""
    # Which engine's position this settlement resolves. A market can hold both a
    # TWAP and a MAJORITY position simultaneously, so the P/L attributed to each
    # engine must be separable — one row per engine, not one row per market.
    engine: str = "TWAP"


@dataclass(slots=True)
class MarketInstance:
    """One market. All of its mutable state, and nothing at module scope.

    Created fresh per market and dropped at close. Never cleared, never reset,
    never reused — there is no reset() method and adding one fails review (A11).
    """

    slug: str
    window_ts: int
    close_ts: int
    phase: MarketPhase = MarketPhase.DISCOVERED
    accumulator: TwapAccumulator = field(default_factory=TwapAccumulator)
    windows: dict[int, ExecutionWindow] = field(default_factory=dict)
    intents: list[ExecutionIntent] = field(default_factory=list)
    orders: list[Order] = field(default_factory=list)
    fills: list[Fill] = field(default_factory=list)
    # Windows admitted but not yet resolved to a fill or a terminal non-fill
    # (hazard H2). Held HERE rather than in a process-level ledger so it is dropped
    # with the instance at close: a reservation that outlived its market would
    # consume the next market's quota and read as a correctly-enforced limit.
    reservations: set[int] = field(default_factory=set)
    settlement: Settlement | None = None
    settlement_twap: Decimal | None = None
    dead_reason: str = ""
    _ptb: Decimal | None = field(default=None, repr=False)

    @classmethod
    def create(cls, window_ts: int, offsets: tuple[int, ...]) -> MarketInstance:
        """Build a market and its windows from the grid timestamp.

        Every default_factory above produces a NEW container per instance. That is
        what keeps two adjacent markets — which are alive simultaneously across a
        close boundary — from sharing an accumulator or an order list.
        """
        instance = cls(
            slug=slug_for(window_ts),
            window_ts=window_ts,
            close_ts=close_ts_for(window_ts),
        )
        for offset in windows_by_priority(offsets):
            instance.windows[offset] = ExecutionWindow(offset_seconds=offset)
        return instance

    # ── PTB ──────────────────────────────────────────────────────────────────
    # Exposed as a read-only property with no setter. The only way in is
    # freeze_ptb(), and it only works once.

    @property
    def ptb(self) -> Decimal | None:
        """The official Price To Beat. Frozen once, immutable for this market."""
        return self._ptb

    def freeze_ptb(self, value: Decimal | int | str) -> None:
        """Capture the official PTB. Raises on the second call, even if identical.

        Refusing an identical re-freeze rather than accepting it as a no-op is
        deliberate: a second call means some code path believes it is allowed to
        refresh the PTB, and the next time that path runs it may carry a different
        value. Every window in this market must use the exact same frozen number
        (A12), so the second call fails loudly while the mistake is still cheap.
        """
        if self._ptb is not None:
            raise ValueError(
                f"PTB for {self.slug} is already frozen at {self._ptb}; "
                "it is captured once and never refreshed"
            )
        price = to_decimal(value)
        if price <= _ZERO:
            raise ValueError(f"PTB must be positive, got {price}")
        self._ptb = price

    def restore_ptb(self, value: Decimal | int | str) -> None:
        """Reload a persisted PTB into a freshly constructed instance.

        Separate from freeze_ptb so that restart recovery does not need a path
        that can overwrite a live PTB. Still refuses to overwrite a set value.
        """
        if self._ptb is not None:
            raise ValueError(f"PTB for {self.slug} is already set; cannot restore over it")
        self._ptb = to_decimal(value)

    # ── observations ─────────────────────────────────────────────────────────

    @property
    def signal_twap(self) -> Decimal | None:
        """ARC's own cumulative mean over this market. The STRATEGY INPUT.

        Not the settlement value and never used as one. The venue settles on its
        own 30-second Chainlink mean (A6).
        """
        return self.accumulator.mean

    @property
    def observation_count(self) -> int:
        return self.accumulator.observation_count

    @property
    def running_sum(self) -> Decimal:
        return self.accumulator.running_sum

    def accepts_observations(self) -> bool:
        return self.phase in _OBSERVING_PHASES

    def add_observation(self, observation: Observation) -> None:
        """Fold an observation into this market's signal TWAP.

        Accepted while SETTLING — those are precisely the observations inside the
        venue's settlement averaging window. Refused while DEAD (no official PTB,
        never traded) or SETTLED (a closed record must not move afterwards).
        """
        if not self.accepts_observations():
            raise ObservationRejectedError(
                f"{self.slug} is {self.phase} and does not accept observations"
            )
        self.accumulator.add(observation.price)

    # ── windows ──────────────────────────────────────────────────────────────

    def windows_by_priority(self) -> tuple[ExecutionWindow, ...]:
        """Windows in priority order: 3, 5, 7, 10, 15."""
        return tuple(self.windows[o] for o in sorted(self.windows))

    def window(self, offset_seconds: int) -> ExecutionWindow:
        return self.windows[offset_seconds]

    def freeze_window(
        self, offset_seconds: int, *, buffer: Decimal, frozen_at: float
    ) -> ExecutionWindow:
        """Freeze one window against THIS market's PTB and current signal TWAP.

        The PTB passed to the window is this instance's frozen value, so all five
        windows necessarily share one number — there is no path by which a later
        window could freeze against a refreshed PTB.
        """
        if self._ptb is None:
            raise WindowFreezeError(
                f"cannot freeze window {offset_seconds}s: {self.slug} has no official PTB"
            )
        twap = self.signal_twap
        if twap is None:
            raise WindowFreezeError(
                f"cannot freeze window {offset_seconds}s: {self.slug} has no observations yet"
            )
        window = self.windows[offset_seconds]
        window.freeze(opening_twap=twap, ptb=self._ptb, buffer=buffer, frozen_at=frozen_at)
        return window

    # ── orders ───────────────────────────────────────────────────────────────

    def live_orders(self) -> tuple[Order, ...]:
        """Orders that may still be resting. Includes INDETERMINATE."""
        return tuple(o for o in self.orders if o.is_live)

    def filled_size_for_window(self, offset_seconds: int) -> Decimal:
        """Cumulative filled quantity across a window's ENTIRE reprice chain.

        Summed across every order for the window, not per order. The trade quota
        decrements only when this reaches the exchange minimum; counting orders
        instead would let five sub-minimum fills open five positions against a
        three-trade budget (hazard H4).
        """
        order_ids = {o.order_id for o in self.orders if o.offset_seconds == offset_seconds}
        total = _ZERO
        for fill in self.fills:
            if fill.order_id in order_ids:
                total += fill.size
        return total

    def directions_held(self) -> frozenset[Direction]:
        """Directions with any filled quantity.

        Feeds the opposing-direction guard: UP at 79c plus DOWN at 22c costs 101c
        and returns exactly 100c — a guaranteed loss, not a hedge (hazard H3).
        """
        held: set[Direction] = set()
        order_direction = {o.order_id: o.direction for o in self.orders}
        for fill in self.fills:
            direction = order_direction.get(fill.order_id)
            if direction is not None and fill.size > _ZERO:
                held.add(direction)
        return frozenset(held)
