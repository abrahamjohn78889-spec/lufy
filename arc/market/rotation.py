"""Market rotation. Markets OVERLAP; settlement never blocks the new market.

At close_ts, two things happen simultaneously (A10/D6):

    market N    live orders cancelled, then settled — IN THE BACKGROUND
    market N+1  PTB frozen and signal TWAP collection STARTS NOW

Settling N is the slow half: it waits on a venue resolution event that may be
seconds away. If N+1 waited for it, N+1 would lose the opening seconds of its own
300-second mean, and the trigger it locks would be computed from a partial window
that silently disagrees with the configured buffer. So settlement is detached and
N+1 begins immediately.

AT MOST TWO instances are live, never three. Asserted, not assumed: a third would
mean a market that should have been archived is still receiving observations, and
the accumulator it is feeding is one nobody will ever read.

Activation is LEVEL-TRIGGERED (A12). This class is driven by `advance(now)`, which
compares the clock against the boundary. Nothing is scheduled on a timer. A
scheduled rotation that fires late leaves the process holding a market that closed,
and one that fires during a suspended process never fires at all; a level check
converges on the correct state from any starting point, including after a restart.

A closed market is persisted, archived, and DEREFERENCED. There is no reset() — a
new market is a new object (A11), so there is no clearing path that can be forgotten
in one of the places that needed it.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final

from arc.clock import Clock
from arc.domain.enums import MarketPhase
from arc.domain.models import MarketInstance, Observation
from arc.domain.timing import window_ts_for
from arc.errors import ObservationRejectedError
from arc.logging_setup import log_event
from arc.storage.store import Store

__all__ = ["MAX_LIVE_MARKETS", "MarketRotator", "RotationEvent"]

# Two: the closing market and the opening one. A third is a bug, and this constant
# exists so the assertion reads as a stated invariant rather than a magic number.
MAX_LIVE_MARKETS: Final[int] = 2


@dataclass(frozen=True, slots=True)
class RotationEvent:
    """What one call to advance() did. Empty fields mean nothing of that kind happened."""

    opened: str = ""
    closed: str = ""
    archived: str = ""

    @property
    def rotated(self) -> bool:
        return bool(self.opened or self.closed)


class MarketRotator:
    """Owns the live MarketInstances and the boundary transitions between them.

    All per-market mutable state lives on the instances this holds, and nothing at
    module scope (A11). Two rotators in one process would be two independent sets of
    markets, which is why the counters below are instance attributes.
    """

    __slots__ = (
        "_clock",
        "_logger",
        "_offsets",
        "_on_settle",
        "_store",
        "closing",
        "current",
        "markets_archived",
        "markets_opened",
        "observations_dropped",
        "observations_routed",
    )

    def __init__(
        self,
        store: Store,
        clock: Clock,
        *,
        offsets: tuple[int, ...],
        on_settle: Callable[[MarketInstance], None] | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self._store = store
        self._clock = clock
        self._offsets = offsets
        # Detached settlement. The callback is invoked and its result is not awaited
        # by the rotation path, which is what keeps N's settlement off N+1's
        # critical path.
        self._on_settle = on_settle
        self._logger = logger
        self.current: MarketInstance | None = None
        self.closing: MarketInstance | None = None
        self.markets_opened = 0
        self.markets_archived = 0
        self.observations_routed = 0
        self.observations_dropped = 0

    # ── invariants ───────────────────────────────────────────────────────────

    @property
    def live(self) -> tuple[MarketInstance, ...]:
        return tuple(m for m in (self.closing, self.current) if m is not None)

    def assert_at_most_two_live(self) -> None:
        """Acceptance criterion 11, checked rather than trusted."""
        count = len(self.live)
        if count > MAX_LIVE_MARKETS:
            raise AssertionError(
                f"{count} live MarketInstances; at most {MAX_LIVE_MARKETS} may exist "
                "(A10/D6) — a third means a closed market was never archived"
            )

    # ── rotation ─────────────────────────────────────────────────────────────

    def advance(self, now: float) -> RotationEvent:
        """Bring the rotator to the correct state for `now`. Level-triggered.

        Idempotent: calling it repeatedly inside one window does nothing. That is
        what makes it safe to call from the main loop at whatever cadence the loop
        happens to run, and what makes recovery after a stall a matter of calling it
        once more rather than of reconstructing missed events.
        """
        window_ts = window_ts_for(now)

        if self.current is not None and self.current.window_ts == window_ts:
            self._reap(now)
            return RotationEvent()

        closed = ""
        archived = ""

        if self.current is not None:
            # The previous market's slot is taken by the one closing now. Anything
            # already sitting there is older still and must be gone before N+1 opens,
            # or the two-live invariant breaks at the boundary.
            if self.closing is not None:
                archived = self._archive(self.closing, now)
            previous = self.current
            closed = previous.slug
            self.closing = previous
            self.current = None
            self._close(previous, now)

        opened = self._open(window_ts, now)
        self.assert_at_most_two_live()
        return RotationEvent(opened=opened, closed=closed, archived=archived)

    def _open(self, window_ts: int, now: float) -> str:
        """Create and persist market N+1. PTB is NOT frozen here.

        Freezing the PTB needs the venue's metadata, which is fetched by discovery;
        wiring it in here would put a network round trip inside the boundary
        transition. The instance exists and is collecting immediately; the PTB is
        frozen onto it by ptb.freeze_ptb_for as soon as metadata is in hand.
        """
        market = MarketInstance.create(window_ts, self._offsets)
        market.phase = MarketPhase.ACTIVE
        self._store.create_market(market, now)
        self._store.save_phase(market.slug, MarketPhase.ACTIVE, now)
        self.current = market
        self.markets_opened += 1
        log_event(
            logging.INFO,
            "Market Opened",
            f"{market.slug}  closes {market.close_ts}",
            logger=self._logger,
        )
        return market.slug

    def _close(self, market: MarketInstance, now: float) -> None:
        """Persist N's state and hand it to settlement. Never blocks.

        The accumulator is written as running_sum + observation_count, not as the
        mean (hazard H1), so a restart mid-settlement resumes the exact sum rather
        than one that has been rounded once already.
        """
        market.phase = MarketPhase.SETTLING
        self._store.save_accumulator(
            market.slug, market.running_sum, market.observation_count, now
        )
        self._store.save_phase(market.slug, MarketPhase.SETTLING, now)
        log_event(
            logging.INFO,
            "Market Closed",
            f"{market.slug}  {market.observation_count} ticks  "
            f"signal_twap {market.signal_twap}",
            logger=self._logger,
        )
        if self._on_settle is not None:
            self._on_settle(market)

    def _reap(self, now: float) -> None:
        """Archive the closing market once it has reached a terminal phase.

        Archiving on phase rather than on elapsed time: a market that has not settled
        yet still has a resolution event coming, and dropping it on a timer would
        lose the outcome the settlement record is for.
        """
        closing = self.closing
        if closing is None:
            return
        if closing.phase in (MarketPhase.SETTLED, MarketPhase.DEAD):
            self._archive(closing, now)

    def _archive(self, market: MarketInstance, now: float) -> str:
        """Persist, archive, and DROP THE REFERENCE (A11).

        Dropping the reference is the operative step. The instance is not cleared for
        reuse; it becomes garbage, and the next market is a different object that
        starts at zero, so there is no state to leak across the boundary.
        """
        self._store.save_accumulator(
            market.slug, market.running_sum, market.observation_count, now
        )
        self._store.archive_market(market.slug, now)
        if self.closing is market:
            self.closing = None
        self.markets_archived += 1
        log_event(logging.INFO, "Market Archived", market.slug, logger=self._logger)
        return market.slug

    def settled(self, slug: str, now: float) -> None:
        """Called when the venue's resolution event lands. Allows archiving.

        Separate from _close so that the outcome always comes from the venue's own
        event and is never inferred from ARC's TWAP (A12).
        """
        for market in self.live:
            if market.slug == slug:
                market.phase = MarketPhase.SETTLED
                self._store.save_phase(slug, MarketPhase.SETTLED, now)
        self._reap(now)

    # ── observations ─────────────────────────────────────────────────────────

    def route(self, observation: Observation) -> tuple[MarketInstance, ...]:
        """Deliver one observation to every live market that will accept it.

        Both markets, at a boundary, deliberately. The instant after close_ts belongs
        to N+1's signal window and also falls inside N's settlement averaging window,
        so a router that picked one would lose an observation at every boundary —
        which is exactly what acceptance criterion 13 checks for.
        """
        accepted: list[MarketInstance] = []
        for market in self.live:
            if not market.accepts_observations():
                continue
            try:
                market.add_observation(observation)
            except ObservationRejectedError:
                continue
            accepted.append(market)

        if accepted:
            self.observations_routed += 1
        else:
            self.observations_dropped += 1
        return tuple(accepted)
