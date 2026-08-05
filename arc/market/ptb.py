"""Price To Beat. Fetched, never computed.

A1 Rule 1, in full force here:

    Fetch the official Price To Beat directly from official Polymarket market
    metadata. Never calculate PTB. Never estimate PTB. Always use the official
    value.

There is no arithmetic in this module that produces a price. No mean, no midpoint,
no interpolation, no last-spot substitution, no carry-forward from the previous
market. The only operation applied to the official value is `to_decimal` on the
exact text the venue sent, and a positivity check.

Two sources, in order:

    L1  the market metadata's own PTB field — the official value
    L2  the previous market's official close reference, and ONLY when the feed was
        continuous across the boundary

L2 is not a calculation. Markets are contiguous (A5), so market N+1's opening
reference is the same instant as market N's close, and if this process observed
that instant on an unbroken connection then the value it recorded IS the official
one — not an estimate of it. The continuity gate is what makes that true: across a
reconnect the process did not observe the boundary, so whatever it holds is a value
from before the gap, and using it would be exactly the estimation A1 forbids.

When both are unavailable the market is marked DEAD, `⛔ PTB Unavailable — no
trading this market` is logged, and the process keeps running. It keeps collecting
observations for that market's slug for the record; it never trades it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Final

from arc.domain.enums import MarketPhase
from arc.domain.models import MarketInstance
from arc.domain.money import to_decimal
from arc.errors import PriceToBeatUnavailableError
from arc.logging_setup import log_event
from arc.market.discovery import MarketMetadata

__all__ = [
    "DEAD_REASON_PTB_UNAVAILABLE",
    "SOURCE_METADATA",
    "SOURCE_PREVIOUS_CLOSE",
    "BoundaryReference",
    "PtbResolution",
    "freeze_ptb_for",
    "resolve_ptb",
]

SOURCE_METADATA: Final[str] = "OFFICIAL_METADATA"
SOURCE_PREVIOUS_CLOSE: Final[str] = "OFFICIAL_PREVIOUS_CLOSE"

DEAD_REASON_PTB_UNAVAILABLE: Final[str] = "PTB_UNAVAILABLE"

_ZERO: Final[Decimal] = Decimal(0)

# The boundary reference is only usable if it was observed within this many
# milliseconds of the boundary instant. A value recorded seconds away from the
# boundary is a nearby price, not the boundary price, and substituting it would be
# an estimate.
_BOUNDARY_TOLERANCE_MS: Final[float] = 1_500.0


@dataclass(frozen=True, slots=True)
class BoundaryReference:
    """The official price observed AT a market boundary on an unbroken connection.

    `continuous` is set by the feed, not inferred here: only the connection itself
    knows whether it stayed up across the boundary instant. A reference carrying
    continuous=False is refused, which is the whole point of recording the flag.
    """

    boundary_ts: int
    price: Decimal
    observed_ts: float
    continuous: bool

    def __post_init__(self) -> None:
        price = to_decimal(self.price)
        if price <= _ZERO:
            raise ValueError(f"boundary reference price must be positive, got {price}")
        object.__setattr__(self, "price", price)

    def usable_for(self, window_ts: int) -> bool:
        """Whether this reference is the official opening value for `window_ts`.

        Three conditions, all required: it is the right boundary, the connection did
        not break across it, and it was actually observed at that instant rather
        than merely near it.
        """
        if not self.continuous:
            return False
        if self.boundary_ts != window_ts:
            return False
        return abs(self.observed_ts - window_ts) * 1000.0 <= _BOUNDARY_TOLERANCE_MS


@dataclass(frozen=True, slots=True)
class PtbResolution:
    """The outcome of resolving a PTB. `value` is None exactly when unavailable."""

    value: Decimal | None
    source: str
    detail: str = ""

    @property
    def available(self) -> bool:
        return self.value is not None


def _official_text_to_decimal(text: str) -> Decimal:
    """Convert the venue's exact PTB text to Decimal. Raises if it is not a price.

    Raising rather than returning a sentinel: an unparseable official value must
    reach the fail-closed path, and a sentinel would need every caller to remember
    to check for it.
    """
    try:
        value = to_decimal(text.strip())
    except (InvalidOperation, ValueError, ArithmeticError) as exc:
        raise PriceToBeatUnavailableError(
            f"official PTB text {text!r} is not a number"
        ) from exc
    if not value.is_finite() or value <= _ZERO:
        raise PriceToBeatUnavailableError(f"official PTB {value} is not a positive price")
    return value


def resolve_ptb(
    metadata: MarketMetadata,
    *,
    window_ts: int,
    boundary: BoundaryReference | None = None,
) -> PtbResolution:
    """Resolve the official PTB for `window_ts`. Never computes one.

    Returns a resolution with `value=None` when no official value is available;
    the caller marks the market DEAD. This function does not mark it dead itself so
    that resolution stays free of side effects and can be exercised directly.
    """
    metadata_detail = "metadata carried no PTB field"
    if metadata.ptb_raw is not None:
        try:
            official = _official_text_to_decimal(metadata.ptb_raw)
        except PriceToBeatUnavailableError as exc:
            # Fall through to L2 rather than dying here: a malformed metadata field
            # is a venue-side defect, and an observed boundary price is still
            # official if the connection held.
            metadata_detail = str(exc)
        else:
            return PtbResolution(
                value=official,
                source=SOURCE_METADATA,
                detail=f"metadata {metadata.ptb_raw}",
            )

    if boundary is not None and boundary.usable_for(window_ts):
        return PtbResolution(
            value=boundary.price,
            source=SOURCE_PREVIOUS_CLOSE,
            detail=f"boundary {boundary.boundary_ts} observed on an unbroken connection",
        )

    if boundary is None:
        gate = "no boundary reference held"
    elif not boundary.continuous:
        gate = "feed was not continuous across the market boundary"
    elif boundary.boundary_ts != window_ts:
        gate = f"boundary reference is for {boundary.boundary_ts}, not {window_ts}"
    else:
        gate = "boundary reference was not observed at the boundary instant"

    return PtbResolution(value=None, source="", detail=f"{metadata_detail}; {gate}")


def freeze_ptb_for(
    market: MarketInstance,
    resolution: PtbResolution,
    *,
    logger: logging.Logger | None = None,
) -> bool:
    """Freeze the resolved PTB onto the market, or mark it DEAD. Returns success.

    Freezing goes through MarketInstance.freeze_ptb, which refuses a second call
    even with an identical value (A11/A12). This function therefore cannot be used
    to refresh a PTB; calling it twice on the same market raises, which is how a
    code path that believes it may re-fetch is caught.
    """
    if not resolution.available or resolution.value is None:
        market.phase = MarketPhase.DEAD
        market.dead_reason = DEAD_REASON_PTB_UNAVAILABLE
        log_event(
            logging.ERROR,
            "PTB Unavailable",
            f"{market.slug} — no trading this market ({resolution.detail})",
            logger=logger,
        )
        return False

    market.freeze_ptb(resolution.value)
    log_event(
        logging.INFO,
        "PTB Frozen",
        f"{resolution.value}  {market.slug}  via {resolution.source}",
        logger=logger,
    )
    return True
