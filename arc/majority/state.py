"""MAJORITY per-market state, and the side lock.

One instance per (market, window), created when the market opens and DROPPED
when it closes. There is no reset(), no clear() and no reuse path — the same
discipline MarketInstance follows (A11), and for the same reason: the dominant
bug class in a five-minute engine is stale state bleeding across a market
boundary, and an object you throw away cannot bleed.

MULTI-WINDOW. A market with N configured MAJORITY windows holds N independent
state objects, each with its own trigger, its own fresh read, its own side lock
and its own intent. State never crosses windows: a 3s window's trigger cannot
fire a 90s window, and a 90s window's side lock cannot pre-empt a 3s window's
determination. The engine's state dict is keyed by `(slug, window_seconds)` for
exactly this reason — two windows with the same slug live under different
keys, and an attempt to read the wrong window's state finds nothing rather than
a sibling's value.

THE SIDE LOCK is the point of this module. `selected_side` is write-once: the only
way in is `select_side()`, it works exactly once, and a second call raises even when
the side is identical. That is deliberate. A second call means some path believes it
may re-determine the side, and the next time that path runs the book will have
moved — so the engine would submit against a side chosen at a different instant than
the one recorded, and nothing would report the difference.

PERSISTENCE. The side lock has to survive a restart. After a crash the in-memory
state dict is empty, so the engine reconstructs it from the persisted
ExecutionIntent (the row's direction is the locked side) before the market's
window opens. `reconstruct_locked_side` does that and only that; the in-memory
sequence picks up from SIDE_SELECTED because that is the state the persisted
intent represents.

This state is ISOLATED from TWAP by construction: it lives on its own object, keyed
by (market slug, window seconds) inside the MAJORITY engine, and no TWAP code path
holds a reference to it. TWAP cannot read it, clear it, or advance it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from typing import Final

from arc.domain.enums import Direction
from arc.errors import ArcError
from arc.majority.trigger import BookSnapshot, MajorityOutcome, MajorityVerdict

__all__ = [
    "MAJORITY_TERMINAL_STATES",
    "MajorityMarketState",
    "MajorityState",
    "MajorityStateError",
]


class MajorityStateError(ArcError):
    """An illegal MAJORITY state operation was attempted.

    Raised rather than ignored: an attempt to re-select a locked side means a code
    path believes it owns a decision that was already made, and that path will keep
    acting on it.
    """


class MajorityState(StrEnum):
    """Where one market's MAJORITY sequence has reached.

    OFF                  the engine is disabled, or fail-closed on its configuration
    ARMED                enabled and armed, no live market window yet
    WAITING_WINDOW       market is live; the execution window has not opened
    WINDOW_OPEN          the window has opened; the book is being monitored
    WAITING_TRIGGER      monitoring, and the threshold has not been reached
    TRIGGERED            the threshold was reached. Exactly once per market
    READING_CLOB         a fresh read was taken for the majority determination
    MAJORITY_DETERMINED  the fresh read yielded UP, DOWN or INDETERMINATE
    SIDE_SELECTED        a tradable side is locked and immutable
    INTENT_CREATED       the immutable intent is persisted
    SUBMITTED            the order reached the venue
    WORKING              resting on the book
    PARTIAL              partially filled
    FILLED               fully filled
    COMPLETED            the record is closed
    NO_TRADE             terminal: INDETERMINATE, or the window closed unfired
    CANCELLED            terminal: retracted
    EXPIRED              terminal: never reached the venue
    REJECTED             terminal: the venue refused it

    WAITING_TRIGGER is kept distinct from WINDOW_OPEN even though both mean "the
    book is being watched", because the operator reading the deck needs to see that
    the window opened at all: a window that never opens and a window that opened and
    was never crossed are different faults with different fixes.
    """

    OFF = "OFF"
    ARMED = "ARMED"
    WAITING_WINDOW = "WAITING_WINDOW"
    WINDOW_OPEN = "WINDOW_OPEN"
    WAITING_TRIGGER = "WAITING_TRIGGER"
    TRIGGERED = "TRIGGERED"
    READING_CLOB = "READING_CLOB"
    MAJORITY_DETERMINED = "MAJORITY_DETERMINED"
    SIDE_SELECTED = "SIDE_SELECTED"
    INTENT_CREATED = "INTENT_CREATED"
    SUBMITTED = "SUBMITTED"
    WORKING = "WORKING"
    PARTIAL = "PARTIAL"
    FILLED = "FILLED"
    COMPLETED = "COMPLETED"
    NO_TRADE = "NO_TRADE"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"
    REJECTED = "REJECTED"


# States from which a market's MAJORITY sequence never advances again. Held as a
# frozenset so a caller can ask the question once rather than matching four members
# by hand and eventually missing one.
MAJORITY_TERMINAL_STATES: Final[frozenset[MajorityState]] = frozenset(
    {
        MajorityState.COMPLETED,
        MajorityState.NO_TRADE,
        MajorityState.CANCELLED,
        MajorityState.EXPIRED,
        MajorityState.REJECTED,
    }
)

# States in which real quantity has executed at the venue. Deliberately NOT terminal
# — a filled order still has to reach settlement, so the sequence continues — but no
# "this market was not traded" outcome may ever be written over one of them, because
# the shares exist whether or not ARC's own record says so.
_MAJORITY_EXECUTED_STATES: Final[frozenset[MajorityState]] = frozenset(
    {MajorityState.PARTIAL, MajorityState.FILLED}
)


@dataclass(slots=True)
class MajorityMarketState:
    """MAJORITY's state for ONE market. Thrown away at close, never reused.

    Every field describing the decision is written exactly once, at the instant it
    is determined, and read verbatim afterwards. Nothing here is recomputed on a
    later pass.
    """

    market_slug: str
    close_ts: int
    execution_window_seconds: int
    state: MajorityState = MajorityState.WAITING_WINDOW
    # The book that satisfied the trigger, and the FRESH book the side was chosen
    # from. Both are kept because they are different evidence: the first explains why
    # the sequence started, the second explains which side was bought. A single
    # snapshot field would make it impossible to audit the two-step rule afterwards.
    trigger_snapshot: BookSnapshot | None = None
    decision_snapshot: BookSnapshot | None = None
    verdict: MajorityVerdict | None = None
    triggered_at: float | None = None
    side_selected_at: float | None = None
    no_trade_reason: str = ""
    # ── entry-condition evidence (spec §6/§8/§9) ─────────────────────────────
    # The entry condition decides WHEN the opportunity fires; it NEVER decides
    # the side. Kept separately from the decision snapshot so the timing
    # evidence (which mode, which BTC levels, which spot crossed) is auditable
    # apart from the book evidence that explains which side was bought.
    entry_mode: str = ""
    btc_reference: Decimal | None = None
    btc_up_trigger: Decimal | None = None
    btc_down_trigger: Decimal | None = None
    fired_level: Decimal | None = None
    fired_spot: Decimal | None = None
    # Final spec §10-§12: with the trigger/target switch ON, the window first
    # waits for the configured Polymarket trigger price to be reached before the
    # buffer condition is evaluated. Latched once — the trigger only decides WHEN
    # the sequence starts; which side is traded comes from the fresh read after
    # the fire, and the latch keeps a book moving back through the trigger level
    # from re-arming or re-evaluating anything.
    price_trigger_reached: bool = False
    _selected_side: Direction | None = field(default=None, repr=False)

    # ── the side lock ────────────────────────────────────────────────────────
    # Exposed as a read-only property with no setter. The only way in is
    # select_side(), and it only works once.

    @property
    def selected_side(self) -> Direction | None:
        """The locked side. None until the majority determination selected one."""
        return self._selected_side

    @property
    def side_locked(self) -> bool:
        return self._selected_side is not None

    def select_side(self, verdict: MajorityVerdict, snapshot: BookSnapshot, now: float) -> None:
        """Lock the side from a fresh determination. Works exactly once.

        Refuses a second call even with an identical side, rather than accepting it
        as a no-op. A repeat call means some path believes the side may be
        re-derived; the next time that path runs, the book will have moved, and the
        engine would then submit a side chosen at an instant nothing recorded.

        Refuses an INDETERMINATE verdict outright: there is no side to lock, and
        writing one would be inventing the decision this whole module exists to
        avoid.
        """
        if self._selected_side is not None:
            raise MajorityStateError(
                f"{self.market_slug} already selected {self._selected_side.value}; "
                "the MAJORITY side is determined once, at the trigger, and is immutable"
            )
        direction = verdict.direction
        if direction is None:
            raise MajorityStateError(
                f"{self.market_slug} cannot lock a side from an "
                f"{MajorityOutcome.INDETERMINATE.value} verdict; the correct outcome is no trade"
            )
        self._selected_side = direction
        self.verdict = verdict
        self.decision_snapshot = snapshot
        self.side_selected_at = now
        self.state = MajorityState.SIDE_SELECTED

    def reconstruct_locked_side(self, direction: Direction, intent_created_at: float) -> None:
        """Materialise a locked side from a persisted ExecutionIntent.

        Called on restart, once per (market, window) that has a persisted intent.
        The persisted intent is the durable half of the same guarantee the
        in-memory lock provides: the side was determined once, at the trigger, and
        the order it authorised is now resting at the venue. A second
        `select_side` call after restart would either be a no-op or, worse, would
        refuse the persisted side and report a NO_TRADE on a market with a real
        resting position — exactly the orphan-position class of bug A4 exists to
        prevent.

        This method only sets the locked side; it does NOT touch the state
        sequence beyond recording that the lock is in effect. The caller advances
        the sequence based on whether the persisted order has been observed and
        whether the market is still in its window.
        """
        self._selected_side = direction
        self.side_selected_at = intent_created_at
        self.state = MajorityState.SIDE_SELECTED

    # ── sequence transitions ─────────────────────────────────────────────────

    @property
    def triggered(self) -> bool:
        """Whether the threshold has already fired for this market.

        The engine checks this before evaluating the trigger again, which is what
        makes the trigger fire exactly once per market: a second firing would take a
        second fresh read and could select a different side than the one already
        locked.
        """
        return self.triggered_at is not None

    @property
    def terminal(self) -> bool:
        return self.state in MAJORITY_TERMINAL_STATES

    def open_window(self) -> None:
        """Note that the execution window has opened. Idempotent by state."""
        if self.state is MajorityState.WAITING_WINDOW:
            self.state = MajorityState.WINDOW_OPEN

    def await_trigger(self) -> None:
        """Note that the book was read and the threshold was not reached."""
        if self.state in (MajorityState.WINDOW_OPEN, MajorityState.WAITING_TRIGGER):
            self.state = MajorityState.WAITING_TRIGGER

    def mark_triggered(self, snapshot: BookSnapshot, now: float) -> None:
        """Record the threshold firing. Refuses a second firing.

        The snapshot that satisfied the trigger is kept, and is deliberately NOT the
        one the side is chosen from — `select_side` takes its own fresh read. Keeping
        it makes the two-step rule auditable rather than merely intended.
        """
        if self.triggered:
            raise MajorityStateError(
                f"{self.market_slug} already triggered at {self.triggered_at}; "
                "the MAJORITY trigger fires once per market"
            )
        self.trigger_snapshot = snapshot
        self.triggered_at = now
        self.state = MajorityState.TRIGGERED

    def mark_reading(self) -> None:
        """Note that the fresh post-trigger read is under way."""
        if self.state is MajorityState.TRIGGERED:
            self.state = MajorityState.READING_CLOB

    def mark_determined(self, verdict: MajorityVerdict, snapshot: BookSnapshot) -> None:
        """Record the determination WITHOUT locking a side.

        Separate from select_side so an INDETERMINATE outcome is recorded with the
        book that produced it and then resolved to NO_TRADE — the evidence for a
        refusal is exactly as valuable as the evidence for a trade.
        """
        self.verdict = verdict
        self.decision_snapshot = snapshot
        self.state = MajorityState.MAJORITY_DETERMINED

    def mark_no_trade(self, reason: str) -> None:
        """Terminal: this market will not be traded by MAJORITY.

        Refuses to overwrite a state that represents REAL EXECUTED QUANTITY, and not
        only an already-terminal one. FILLED and PARTIAL are not terminal — a filled
        order still has to reach settlement — but quantity has executed at the venue
        by then, and writing NO_TRADE over either would erase a position ARC actually
        holds from the engine's own view of the world. The deck would show a market
        that was never traded while the venue held shares against it.
        """
        if self.terminal or self.state in _MAJORITY_EXECUTED_STATES:
            return
        self.state = MajorityState.NO_TRADE
        self.no_trade_reason = reason

    def mark_intent_created(self) -> None:
        if self.state is MajorityState.SIDE_SELECTED:
            self.state = MajorityState.INTENT_CREATED

    def mark_state(self, state: MajorityState) -> None:
        """Set an execution-phase state from the order lifecycle.

        The order states are owned by the execution machinery, which is shared with
        TWAP and is the authority on what an order is doing. This mirrors that
        authority onto the engine's view rather than deciding it independently — two
        state machines over one order would eventually disagree, and the deck would
        show whichever was consulted.
        """
        if self.terminal:
            return
        self.state = state

    def describe(self) -> str:
        """One line for the deck and the log."""
        side = "-" if self._selected_side is None else self._selected_side.value
        book = "-" if self.decision_snapshot is None else self.decision_snapshot.describe()
        mode = self.entry_mode or "-"
        return f"{self.market_slug}  {self.state.value}  mode {mode}  side {side}  {book}"
