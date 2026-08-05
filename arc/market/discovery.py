"""Market discovery: slug math, then the venue's own metadata.

The slug is computed locally because it has to be known before any request can be
made (A5):

    window_ts = floor(now / 300) * 300
    slug      = f"btc-updown-5m-{window_ts}"
    close_ts  = window_ts + 300

The venue's metadata is then fetched and the venue's close_ts WINS. When the two
disagree the local arithmetic is not silently preferred and the market is not
skipped: the divergence is logged as SLUG_MATH_DIVERGENCE and the venue's value is
used. The venue settles the market, so the venue's clock is the one that decides
when it closes; a bot that trusted its own arithmetic over the venue's would cancel
and settle at the wrong instant and the error would look like a latency problem.

Nothing here computes a Price To Beat. `ptb` arrives as an opaque string from the
metadata and is handed on untouched (see ptb.py).
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, Final

import httpx

from arc.domain.timing import close_ts_for, next_window_ts, slug_for, window_ts_for
from arc.errors import FeedError
from arc.logging_setup import log_event

__all__ = [
    "GAMMA_MARKETS_URL",
    "DiscoveredMarket",
    "MarketDiscovery",
    "MarketMetadata",
    "SlugMath",
    "next_slug_math",
    "open_discovery",
    "parse_market_metadata",
    "slug_math",
]

# The public Gamma metadata endpoint. Read-only, unauthenticated: discovery must
# work in V1 paper mode where no credentials exist at all.
GAMMA_MARKETS_URL: Final[str] = "https://gamma-api.polymarket.com/markets"

_REQUEST_TIMEOUT_SECONDS: Final[float] = 10.0

# Metadata key spellings the Gamma payload is known to use. Tolerant lookup only:
# a payload carrying none of them yields None and the caller fails closed. No key
# is defaulted to a value.
_CLOSE_TS_KEYS: Final[tuple[str, ...]] = ("closeTime", "close_ts", "endDateTs", "gameStartTime")
_CONDITION_KEYS: Final[tuple[str, ...]] = ("conditionId", "condition_id")
_TOKEN_KEYS: Final[tuple[str, ...]] = ("clobTokenIds", "clob_token_ids", "tokens")
_PTB_KEYS: Final[tuple[str, ...]] = ("priceToBeat", "price_to_beat", "strikePrice", "openingPrice")
_ACTIVE_KEYS: Final[tuple[str, ...]] = ("active",)
_CLOSED_KEYS: Final[tuple[str, ...]] = ("closed",)

_MS_THRESHOLD: Final[float] = 1e11


@dataclass(frozen=True, slots=True)
class SlugMath:
    """The locally computed identity of a market. Pure arithmetic, no I/O."""

    window_ts: int
    close_ts: int
    slug: str


def slug_math(now: float) -> SlugMath:
    """The market identity for `now`, per A5. Never guesses; never rounds up."""
    window_ts = window_ts_for(now)
    return SlugMath(
        window_ts=window_ts,
        close_ts=close_ts_for(window_ts),
        slug=slug_for(window_ts),
    )


def next_slug_math(window_ts: int) -> SlugMath:
    """The identity of the market that opens when `window_ts`'s market closes.

    Markets are CONTIGUOUS (A5): the next window_ts equals this market's close_ts.
    Prefetching the next market's metadata before the boundary is what lets N+1
    freeze its PTB the instant N closes, rather than after a round trip that would
    lose the first seconds of its signal TWAP.
    """
    upcoming = next_window_ts(window_ts)
    return SlugMath(
        window_ts=upcoming,
        close_ts=close_ts_for(upcoming),
        slug=slug_for(upcoming),
    )


@dataclass(frozen=True, slots=True)
class MarketMetadata:
    """What the venue says about a market.

    `venue_close_ts` and `ptb_raw` are None when the payload did not carry them.
    None is preserved rather than filled in: a missing PTB must reach the fail-closed
    path in ptb.py, and a missing close time must not be replaced by local
    arithmetic that the divergence check would then always agree with.

    `ptb_raw` is TEXT. The metadata value is carried as the exact characters the
    venue sent, so that converting it to Decimal happens once, at the point of use,
    with no float in between.
    """

    slug: str
    condition_id: str
    token_ids: tuple[str, ...]
    venue_close_ts: int | None
    ptb_raw: str | None
    active: bool
    closed: bool
    raw: dict[str, Any]


def _first_key(payload: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        if key in payload and payload[key] is not None:
            return payload[key]
    return None


def _as_epoch_seconds(raw: Any) -> int | None:
    """Read a venue timestamp as epoch seconds, or None if it is not one."""
    if isinstance(raw, bool) or not isinstance(raw, (int, float, str)):
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    if value != value or value in (float("inf"), float("-inf")):
        return None
    if value >= _MS_THRESHOLD:
        value = value / 1000.0
    if value <= 0:
        return None
    return int(value)


def _as_token_ids(raw: Any) -> tuple[str, ...]:
    """Token IDs as strings. Gamma sends them either as a list or as a JSON string."""
    if raw is None:
        return ()
    if isinstance(raw, str):
        text = raw.strip()
        if text.startswith("["):
            try:
                parsed = json.loads(text)
            except ValueError:
                return ()
            return tuple(str(item) for item in parsed if item is not None)
        return (text,) if text else ()
    if isinstance(raw, (list, tuple)):
        ids: list[str] = []
        for item in raw:
            if isinstance(item, dict):
                value = _first_key(item, ("token_id", "tokenId", "id"))
                if value is not None:
                    ids.append(str(value))
            elif item is not None:
                ids.append(str(item))
        return tuple(ids)
    return ()


def _as_bool(raw: Any, *, default: bool) -> bool:
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str):
        lowered = raw.strip().lower()
        if lowered in ("true", "1", "yes"):
            return True
        if lowered in ("false", "0", "no"):
            return False
    return default


def _as_ptb_text(raw: Any) -> str | None:
    """The metadata PTB as exact text, or None.

    A float is refused rather than stringified. `120000.05` arriving as a JSON
    number is already binary-rounded, and `str()` of it would preserve the rounding
    while looking like an exact value — which is precisely the failure that A1's
    "always use the official value" is guarding against.
    """
    if raw is None:
        return None
    if isinstance(raw, float):
        return None
    if isinstance(raw, bool):
        return None
    if isinstance(raw, (int, str)):
        text = str(raw).strip()
        return text or None
    return None


def parse_market_metadata(payload: object, *, slug: str) -> MarketMetadata:
    """Turn one Gamma market object into MarketMetadata. Raises FeedError if unusable.

    A market with no condition_id cannot be traded or settled against, so it is a
    hard failure rather than a partially populated record.
    """
    if not isinstance(payload, dict):
        raise FeedError(f"metadata for {slug} is not an object: {type(payload)}")

    condition_id = _first_key(payload, _CONDITION_KEYS)
    if not isinstance(condition_id, str) or not condition_id.strip():
        raise FeedError(f"metadata for {slug} carries no conditionId")

    return MarketMetadata(
        slug=slug,
        condition_id=condition_id.strip(),
        token_ids=_as_token_ids(_first_key(payload, _TOKEN_KEYS)),
        venue_close_ts=_as_epoch_seconds(_first_key(payload, _CLOSE_TS_KEYS)),
        ptb_raw=_as_ptb_text(_first_key(payload, _PTB_KEYS)),
        active=_as_bool(_first_key(payload, _ACTIVE_KEYS), default=True),
        closed=_as_bool(_first_key(payload, _CLOSED_KEYS), default=False),
        raw=payload,
    )


@dataclass(frozen=True, slots=True)
class DiscoveredMarket:
    """A market's identity after the venue has been consulted.

    `close_ts` is the AUTHORITATIVE value: the venue's, when it supplied one. The
    locally computed value is kept alongside it only so the divergence is
    inspectable after the fact.
    """

    slug: str
    window_ts: int
    close_ts: int
    computed_close_ts: int
    metadata: MarketMetadata

    @property
    def diverged(self) -> bool:
        return self.close_ts != self.computed_close_ts


class MarketDiscovery:
    """Fetches venue metadata for a slug. Holds one HTTP client.

    The client is injected rather than constructed here so a test can drive every
    branch — including a venue that disagrees about close_ts — without a network.
    """

    __slots__ = ("_client", "_logger", "_url")

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        url: str = GAMMA_MARKETS_URL,
        logger: logging.Logger | None = None,
    ) -> None:
        self._client = client
        self._url = url
        self._logger = logger

    async def fetch_metadata(self, slug: str) -> MarketMetadata:
        """Fetch one market's metadata by slug. Raises FeedError on any failure.

        Every transport failure becomes FeedError, which is operational, not fatal:
        a metadata endpoint that is briefly unreachable must leave the process alive
        with its dashboard and its recorded observations intact (A8).
        """
        try:
            response = await self._client.get(
                self._url, params={"slug": slug}, timeout=_REQUEST_TIMEOUT_SECONDS
            )
            response.raise_for_status()
            body = response.json()
        except httpx.HTTPError as exc:
            raise FeedError(f"metadata request for {slug} failed: {exc}") from exc
        except ValueError as exc:
            raise FeedError(f"metadata for {slug} was not valid JSON: {exc}") from exc

        if isinstance(body, dict):
            candidates = body.get("data", body.get("markets", [body]))
        else:
            candidates = body
        if not isinstance(candidates, list) or not candidates:
            raise FeedError(f"venue returned no market for {slug}")

        return parse_market_metadata(candidates[0], slug=slug)

    async def discover(self, now: float) -> DiscoveredMarket:
        """Discover the market covering `now`."""
        return await self._discover(slug_math(now))

    async def prefetch_next(self, window_ts: int) -> DiscoveredMarket:
        """Discover the market that opens at this market's close.

        Called before the boundary so N+1's condition_id and PTB are in hand when N
        closes. Markets are contiguous, so there is no gap to wait through.
        """
        return await self._discover(next_slug_math(window_ts))

    async def _discover(self, math: SlugMath) -> DiscoveredMarket:
        metadata = await self.fetch_metadata(math.slug)
        close_ts = math.close_ts

        if metadata.venue_close_ts is not None and metadata.venue_close_ts != math.close_ts:
            close_ts = metadata.venue_close_ts
            log_event(
                logging.WARNING,
                "SLUG_MATH_DIVERGENCE",
                f"{math.slug}  computed {math.close_ts}  venue {metadata.venue_close_ts}  "
                "(using venue)",
                logger=self._logger,
            )

        return DiscoveredMarket(
            slug=math.slug,
            window_ts=math.window_ts,
            close_ts=close_ts,
            computed_close_ts=math.close_ts,
            metadata=metadata,
        )


@asynccontextmanager
async def open_discovery(
    *,
    url: str = GAMMA_MARKETS_URL,
    logger: logging.Logger | None = None,
) -> AsyncIterator[MarketDiscovery]:
    """Own an HTTP client for the duration of a run and yield a discovery on it.

    The client is constructed HERE rather than in the runtime, so that `httpx` is
    named only inside arc/market/. Every layer above this package stays reachable
    from a test with no network at all, which is the property the structural gate
    asserts — and the property that makes the runtime's behaviour reproducible
    without a venue.
    """
    async with httpx.AsyncClient() as client:
        yield MarketDiscovery(client, url=url, logger=logger)
