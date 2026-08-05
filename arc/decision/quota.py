"""Trade quota: what is USED, what is RESERVED, and when a trade counts.

Two hazards live here.

H2 — over-commitment between admission and fill. A quota that counted only filled
trades would let three windows all pass a "2 of 3 used" check inside the same
second, before any of them fills, and open four positions against a three-trade
budget. So admission RESERVES, and the reservation is released only when the window
reaches a terminal non-fill. Reservations are counted alongside used trades at the
gate.

H4 — counting orders instead of quantity. The quota decrements only when the
CUMULATIVE FILLED QUANTITY across a window's entire reprice chain reaches the
exchange minimum. A reprice is cancel-then-place, so one logical position produces
several order ids; counting orders would let five sub-minimum fills consume five
trades of budget, and counting the first order of a chain would consume a trade for
a position that was never actually opened.

Reservations live on the MarketInstance, not here (A11). This class is a stateless
reader plus two one-line mutators, so there is no per-market state at module scope
and nothing that survives a market boundary.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Final

from arc.domain.models import MarketInstance

__all__ = ["QuotaLedger", "QuotaSnapshot"]

_ZERO: Final[Decimal] = Decimal("0")


@dataclass(frozen=True, slots=True)
class QuotaSnapshot:
    """The quota position for one market at one instant."""

    used: int
    reserved: int
    limit: int

    @property
    def committed(self) -> int:
        return self.used + self.reserved

    @property
    def available(self) -> int:
        remaining = self.limit - self.committed
        return remaining if remaining > 0 else 0

    @property
    def exhausted(self) -> bool:
        return self.available == 0


class QuotaLedger:
    """Counts used and reserved trades for whichever market it is given.

    Holds only the exchange minimum and the per-market limit — both configuration.
    Every per-market number is read from the instance passed in, so one ledger
    correctly serves both markets that are alive across a close boundary (D6).
    """

    __slots__ = ("_max_trades", "_min_tradable_size")

    def __init__(self, *, max_trades_per_market: int, min_tradable_size: Decimal) -> None:
        if max_trades_per_market <= 0:
            raise ValueError(
                f"max_trades_per_market must be at least 1, got {max_trades_per_market}"
            )
        if min_tradable_size <= _ZERO:
            raise ValueError(f"min_tradable_size must be positive, got {min_tradable_size}")
        self._max_trades = max_trades_per_market
        self._min_tradable_size = min_tradable_size

    @property
    def limit(self) -> int:
        return self._max_trades

    @property
    def min_tradable_size(self) -> Decimal:
        return self._min_tradable_size

    # ── counting ─────────────────────────────────────────────────────────────

    def counts(self, market: MarketInstance, offset_seconds: int) -> bool:
        """Does this window's cumulative filled quantity reach the minimum? (H4)

        Summed across the window's whole reprice chain by
        `filled_size_for_window`, not per order.
        """
        return market.filled_size_for_window(offset_seconds) >= self._min_tradable_size

    def used(self, market: MarketInstance) -> int:
        """Windows whose cumulative fills reached the exchange minimum.

        Recomputed from the fills each time rather than kept as a counter. A
        counter would need incrementing at exactly one point in the fill path, and
        a redelivered fill or a missed increment would put it permanently out of
        step with the fills that are actually on disk — with nothing to detect the
        divergence.
        """
        return sum(
            1
            for window in market.windows_by_priority()
            if self.counts(market, window.offset_seconds)
        )

    def reserved(self, market: MarketInstance) -> int:
        """Admitted windows not yet resolved to a fill or a terminal non-fill (H2).

        A window that has both a reservation AND countable fills is counted once,
        as used: leaving the reservation in the total would double-charge the
        budget for a single trade.
        """
        return sum(
            1
            for offset in market.reservations
            if not self.counts(market, offset)
        )

    def snapshot(self, market: MarketInstance) -> QuotaSnapshot:
        return QuotaSnapshot(
            used=self.used(market), reserved=self.reserved(market), limit=self._max_trades
        )

    # ── mutation ─────────────────────────────────────────────────────────────

    def reserve(self, market: MarketInstance, offset_seconds: int) -> bool:
        """Claim a slot for a window at admission. False if already reserved.

        Idempotent by set semantics, so a duplicated decision pass cannot consume
        two slots for one window.
        """
        if offset_seconds in market.reservations:
            return False
        market.reservations.add(offset_seconds)
        return True

    def release(self, market: MarketInstance, offset_seconds: int) -> bool:
        """Return a slot on a terminal NON-FILL. False if there was none.

        Called when an order is cancelled, rejected or expires with cumulative
        fills below the exchange minimum. Never called on a fill: a filled trade
        moves from reserved to used, and releasing it would hand the budget back
        for a position that is actually open.
        """
        if offset_seconds not in market.reservations:
            return False
        market.reservations.discard(offset_seconds)
        return True
