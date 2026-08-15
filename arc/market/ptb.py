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
    L2  the venue's PUBLISHED `finalPrice` for the previous market, cached when it
        became available

L2 is not a calculation and not an observation. Live measurement on 2026-08-05
established, on six consecutive settled markets with zero mismatches, that

    priceToBeat(M) == finalPrice(M-1)

exactly. Markets are contiguous (A5), so market M-1's close instant IS market M's
window_ts, and the venue publishes its own number for that instant. The venue writes
`eventMetadata` — both `priceToBeat` and `finalPrice` — roughly 25 seconds after a
market closes, which is roughly 25 seconds INTO the next market's life: 260 seconds
before that market's earliest execution window at close-15s. So M's official opening
reference is readable from the venue long before M needs it.

Reading it is a lookup of an official venue value. It is NOT:

  * ARC's own boundary observation. That was measured against the venue's published
    number and differed by 6E-12 — genuinely, not as a decoding artifact. Close is
    not official, and substituting it would be the estimation A1 forbids. The
    boundary path is therefore gone from this module entirely.
  * arithmetic. Nothing is averaged, interpolated, carried forward from a spot price,
    or derived. The cached text is the venue's characters, converted once.

When both sources are unavailable the market is marked DEAD, `⛔ PTB Unavailable — no
trading this market` is logged, and the process keeps running. It keeps collecting
observations for that market's slug for the record; it never trades it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Final

from arc.domain.models import MarketInstance
from arc.domain.money import to_decimal
from arc.domain.timing import MARKET_DURATION_SECONDS
from arc.errors import PriceToBeatUnavailableError
from arc.logging_setup import log_event
from arc.market.discovery import MarketMetadata

__all__ = [
    "DEAD_REASON_PTB_UNAVAILABLE",
    "SOURCE_METADATA",
    "SOURCE_PREVIOUS_CLOSE",
    "PreviousClosePtb",
    "PreviousClosePtbCache",
    "PtbResolution",
    "freeze_ptb_for",
    "resolve_ptb",
]

SOURCE_METADATA: Final[str] = "OFFICIAL_METADATA"
SOURCE_PREVIOUS_CLOSE: Final[str] = "OFFICIAL_PREVIOUS_CLOSE"

DEAD_REASON_PTB_UNAVAILABLE: Final[str] = "PTB_UNAVAILABLE"

_ZERO: Final[Decimal] = Decimal(0)


@dataclass(frozen=True, slots=True)
class PreviousClosePtb:
    """A settled market's venue-published final price, and the window it opens.

    `opens_window_ts` is stored rather than recomputed at every read so that the
    identity being asserted — this value belongs to exactly one window — is fixed at
    the moment of capture. A cache that recomputed it could be asked about the wrong
    window and answer.
    """

    settled_window_ts: int
    opens_window_ts: int
    price: Decimal
    raw: str

    def __post_init__(self) -> None:
        if self.opens_window_ts != self.settled_window_ts + MARKET_DURATION_SECONDS:
            raise ValueError(
                f"{self.settled_window_ts} does not close at {self.opens_window_ts}"
            )
        if not self.price.is_finite() or self.price <= _ZERO:
            raise ValueError(f"published final price must be positive, got {self.price}")

    def usable_for(self, window_ts: int) -> bool:
        """Whether this is the official opening reference for `window_ts`.

        One condition only, and it is exact. There is no tolerance to apply: the
        value was published by the venue for a specific market, not observed near a
        moment in time.
        """
        return self.opens_window_ts == window_ts


class PreviousClosePtbCache:
    """Holds the most recent settled market's published final price.

    Only the latest entry is kept. A market's PTB is frozen once, at its own opening,
    and every earlier entry belongs to a market that has already been frozen or has
    already died — retaining them would grow without bound across a 24/7 run for
    values that can never be read again.

    Instance state, not module state (A11): two runs in one process must not share a
    cached price.
    """

    __slots__ = ("_latest",)

    def __init__(self) -> None:
        self._latest: PreviousClosePtb | None = None

    @property
    def latest(self) -> PreviousClosePtb | None:
        return self._latest

    def offer(self, metadata: MarketMetadata, *, settled_window_ts: int) -> PreviousClosePtb | None:
        """Cache a settled market's published final price. Returns the entry, or None.

        None when the venue has not published `finalPrice` yet — which is the case for
        a market's entire life — or when the text is not a positive price. Returning
        None rather than raising because a not-yet-published field is the normal
        state, not an error, and the caller polls.

        An older entry never replaces a newer one, and re-offering the current entry
        is idempotent. Metadata fetches are not ordered: a slow response for market
        M-2 can land after M-1's, and letting it win would hand the next market the
        wrong window's reference.
        """
        if metadata.final_price_raw is None:
            return None
        try:
            price = _official_text_to_decimal(metadata.final_price_raw)
        except PriceToBeatUnavailableError:
            return None

        held = self._latest
        if held is not None:
            if held.settled_window_ts > settled_window_ts:
                return None
            if held.settled_window_ts == settled_window_ts:
                return held

        entry = PreviousClosePtb(
            settled_window_ts=settled_window_ts,
            opens_window_ts=settled_window_ts + MARKET_DURATION_SECONDS,
            price=price,
            raw=metadata.final_price_raw,
        )
        self._latest = entry
        return entry

    def for_window(self, window_ts: int) -> PreviousClosePtb | None:
        """The cached entry that opens `window_ts`, or None."""
        if self._latest is not None and self._latest.usable_for(window_ts):
            return self._latest
        return None


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
    previous_close: PreviousClosePtb | None = None,
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
            # Fall through to L2 rather than dying here: a malformed metadata field is
            # a venue-side defect, and the previous market's published final price is
            # a different, independently published official value.
            metadata_detail = str(exc)
        else:
            return PtbResolution(
                value=official,
                source=SOURCE_METADATA,
                detail=f"metadata {metadata.ptb_raw}",
            )

    if previous_close is not None and previous_close.usable_for(window_ts):
        return PtbResolution(
            value=previous_close.price,
            source=SOURCE_PREVIOUS_CLOSE,
            detail=(
                f"published finalPrice {previous_close.raw} "
                f"of market {previous_close.settled_window_ts}"
            ),
        )

    if previous_close is None:
        gate = "previous market's finalPrice not published yet"
    else:
        gate = (
            f"cached finalPrice opens {previous_close.opens_window_ts}, not {window_ts}"
        )

    return PtbResolution(value=None, source="", detail=f"{metadata_detail}; {gate}")


def freeze_ptb_for(
    market: MarketInstance,
    resolution: PtbResolution,
    *,
    logger: logging.Logger | None = None,
) -> bool:
    """Freeze the resolved PTB onto the market. Returns success.

    PTB is display-only (user directive). Missing PTB no longer kills the market;
    it stays tradable. Freezing goes through MarketInstance.freeze_ptb, which
    refuses a second call even with an identical value (A11/A12).
    """
    if not resolution.available or resolution.value is None:
        # PTB is display-only — never kill the market on missing PTB.
        log_event(
            logging.INFO,
            "PTB Not Yet Available",
            f"{market.slug} — PTB missing but market stays active ({resolution.detail})",
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
