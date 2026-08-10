"""The MAJORITY trigger and the MAJORITY determination. Two SEPARATE operations.

Both are pure functions of a book snapshot. Nothing here reads a clock, opens a
socket, or touches the store — which is what makes the whole decision testable by
handing it two numbers.

THE TRIGGER is an activation threshold on the Polymarket outcome-share book:

    max(best_bid(UP), best_bid(DOWN)) >= trigger_price          inclusive

THE MAJORITY is a separate comparison, made against a FRESH read taken after the
trigger fired:

    UP   when best_bid(UP) > best_bid(DOWN)
    DOWN when best_bid(DOWN) > best_bid(UP)
    INDETERMINATE otherwise — equal, either side missing, stale, or unreadable

WHY THEY ARE SEPARATE. The side that happened to cross the trigger is NOT the side
that gets bought. A trigger at 0.90 can fire because UP touched 0.90, and the fresh
read a moment later can show UP 0.87 / DOWN 0.91 — in which case the correct side
is DOWN. Collapsing the two steps into "buy whichever side crossed" would buy the
losing side in exactly the case the two-step rule exists to handle.

The trigger does NOT have to remain satisfied afterwards. It means "make the
majority determination now", nothing more. A fresh read of UP 0.16 / DOWN 0.85 —
both below a 0.90 trigger — still yields DOWN, because the trigger already did its
job when it fired.

FORBIDDEN INPUTS. Last trade, midpoint, asks, ask quantity, bid quantity, displayed
depth, historical or cached majority. The only quantity read is the best resting bid
on each outcome's own book, which is what `Executor.best_price` returns. That is not
a simplification of a richer rule — it IS the rule.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Final

from arc.domain.enums import Direction
from arc.domain.money import dec_str

__all__ = [
    "BookSnapshot",
    "MajorityOutcome",
    "MajorityVerdict",
    "determine_majority",
    "is_triggered",
    "trigger_value",
]

_ZERO: Final[Decimal] = Decimal("0")


class MajorityOutcome(StrEnum):
    """The result of one majority determination.

    INDETERMINATE is a first-class outcome, not an error and not a tie to be
    broken. Every condition that folds into it — equal bids, a missing side, a
    stale read, a failed read — has the same correct response: no trade. Resolving
    it to a side would open a real position on a book that expressed no preference,
    in whichever direction the tie-break happened to name.
    """

    UP = "UP"
    DOWN = "DOWN"
    INDETERMINATE = "INDETERMINATE"

    @property
    def direction(self) -> Direction | None:
        """The tradable side, or None for INDETERMINATE."""
        if self is MajorityOutcome.UP:
            return Direction.UP
        if self is MajorityOutcome.DOWN:
            return Direction.DOWN
        return None


@dataclass(frozen=True, slots=True)
class BookSnapshot:
    """Both sides of one market's book, read at one instant, with its freshness.

    Frozen and carried as a unit so the trigger comparison and the majority
    comparison provably see the SAME two numbers. Two separate reads — one per side,
    or one per comparison — would let the book move between them, and the resulting
    side would depend on how long the pass took.

    `fresh` is the caller's freshness verdict rather than an age computed here: this
    module has no clock (the runtime owns the one authoritative clock), and a second
    staleness rule would eventually disagree with the first.

    A side that is None means the venue published no bid for that outcome. It is
    NOT zero: an outcome nobody is bidding on and an outcome bid at zero would
    compare identically, and a missing side must never win a comparison.
    """

    best_bid_up: Decimal | None
    best_bid_down: Decimal | None
    read_at: float
    fresh: bool = True

    @property
    def complete(self) -> bool:
        """Whether both sides carry a usable bid."""
        return self.best_bid_up is not None and self.best_bid_down is not None

    @property
    def usable(self) -> bool:
        """Whether this snapshot may be compared at all."""
        return self.fresh and self.complete

    def describe(self) -> str:
        """Both prices, for the log and the deck. Never a computed midpoint."""
        up = "-" if self.best_bid_up is None else dec_str(self.best_bid_up)
        down = "-" if self.best_bid_down is None else dec_str(self.best_bid_down)
        return f"UP {up}  DOWN {down}"


@dataclass(frozen=True, slots=True)
class MajorityVerdict:
    """The determination, with the exact numbers it was made from.

    The two bids are carried on the verdict so the recorded decision can be audited
    against the book that produced it. Recomputing them later would read a book that
    has since moved, which is the whole failure the frozen snapshot prevents.
    """

    outcome: MajorityOutcome
    best_bid_up: Decimal | None
    best_bid_down: Decimal | None
    reason: str = ""

    @property
    def tradable(self) -> bool:
        return self.outcome is not MajorityOutcome.INDETERMINATE

    @property
    def direction(self) -> Direction | None:
        return self.outcome.direction


def trigger_value(snapshot: BookSnapshot) -> Decimal | None:
    """`max(best_bid(UP), best_bid(DOWN))`, or None when it cannot be formed.

    None when EITHER side is missing, deliberately — not the one present bid. A
    market quoting only one side is a market ARC cannot compare, and taking the
    single available number as the maximum would let the trigger fire on a book that
    could never yield a majority.
    """
    if snapshot.best_bid_up is None or snapshot.best_bid_down is None:
        return None
    return max(snapshot.best_bid_up, snapshot.best_bid_down)


def is_triggered(snapshot: BookSnapshot, trigger_price: Decimal) -> bool:
    """Whether the book has reached the configured activation threshold.

    INCLUSIVE: `>=`. The configured trigger IS the threshold, and landing exactly on
    it is reaching it. Requiring an overshoot would silently discard every exact
    touch — and 0.90 exactly is the ordinary case on a tick-sized book, not an edge
    case.

    A stale or incomplete snapshot NEVER triggers. Firing on a stale book would
    start the decision sequence from a price that has already moved, and the fresh
    read that follows would be answering a question the current book never asked.
    """
    if not snapshot.usable:
        return False
    best = trigger_value(snapshot)
    if best is None:
        return False
    return best >= trigger_price


def determine_majority(snapshot: BookSnapshot) -> MajorityVerdict:
    """Compare the two best bids. STRICT comparison; equality is not a side.

    Called with a FRESH snapshot taken after the trigger fired — never with the
    snapshot that satisfied the trigger. The caller enforces that ordering; this
    function only guarantees that whatever it is handed is compared honestly.

    Strictness matters here for the same reason it does in TWAP's direction
    contract: equal bids mean the book expressed no preference, and `>=` would
    resolve every tie to UP — opening real positions on markets that were genuinely
    balanced, in a direction nothing chose.
    """
    if not snapshot.fresh:
        return MajorityVerdict(
            outcome=MajorityOutcome.INDETERMINATE,
            best_bid_up=snapshot.best_bid_up,
            best_bid_down=snapshot.best_bid_down,
            reason="the book read is stale; a majority from a stale book is not a majority",
        )

    up, down = snapshot.best_bid_up, snapshot.best_bid_down

    if up is None and down is None:
        return MajorityVerdict(
            outcome=MajorityOutcome.INDETERMINATE,
            best_bid_up=None,
            best_bid_down=None,
            reason="neither side has a resting bid",
        )
    if up is None:
        return MajorityVerdict(
            outcome=MajorityOutcome.INDETERMINATE,
            best_bid_up=None,
            best_bid_down=down,
            reason="UP has no resting bid, so the two sides cannot be compared",
        )
    if down is None:
        return MajorityVerdict(
            outcome=MajorityOutcome.INDETERMINATE,
            best_bid_up=up,
            best_bid_down=None,
            reason="DOWN has no resting bid, so the two sides cannot be compared",
        )

    if up > down:
        return MajorityVerdict(
            outcome=MajorityOutcome.UP,
            best_bid_up=up,
            best_bid_down=down,
            reason=f"UP {dec_str(up)} > DOWN {dec_str(down)}",
        )
    if down > up:
        return MajorityVerdict(
            outcome=MajorityOutcome.DOWN,
            best_bid_up=up,
            best_bid_down=down,
            reason=f"DOWN {dec_str(down)} > UP {dec_str(up)}",
        )
    return MajorityVerdict(
        outcome=MajorityOutcome.INDETERMINATE,
        best_bid_up=up,
        best_bid_down=down,
        reason=(
            f"both sides bid {dec_str(up)}; the book expresses no majority and "
            "equality is not a tie to be broken"
        ),
    )
