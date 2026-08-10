"""Price-stream provider selection. The rest of ARC never learns which one is live.

One interface, chosen by configuration alone. The PTB Engine, Window Engine, Decision
Engine, Risk Engine, Limit Order Engine and dashboard all receive observations that
carry no trace of their origin, so swapping providers changes no strategy code and no
strategy behaviour: only the source of the price data moves.

RTDS is the default. Chainlink is implemented against the official Data Streams
documentation (see market/chainlink.py) and requires credentials, a stream ID and
the stream's decimal scale; each missing item is refused by name at startup rather
than defaulted, because every plausible default here produces prices that look
real. Nothing about the choice reaches the strategy path: both providers yield the
same frame shape, so A21's grep gate holds.

The interface is stated as a Protocol rather than a base class so RtdsFeed does not
have to inherit anything. The one thing every provider must do is yield raw frames and
report whether it is connected; parsing and validation stay in validation.py, in one
place, for every provider.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from enum import StrEnum
from typing import Protocol, runtime_checkable

from arc.clock import Clock
from arc.errors import ConfigInvariantError
from arc.market.chainlink import build_chainlink_feed
from arc.market.feed import RTDS_URL, BackoffPolicy, RtdsFeed

__all__ = ["ProviderName", "TwapProvider", "build_provider"]


class ProviderName(StrEnum):
    """The providers that exist as configuration values."""

    RTDS = "RTDS"
    CHAINLINK = "CHAINLINK"


@runtime_checkable
class TwapProvider(Protocol):
    """A live price stream. Yields raw frames; validates nothing."""

    @property
    def url(self) -> str: ...

    @property
    def connect_attempts(self) -> int: ...

    @property
    def disconnects(self) -> int: ...

    def messages(self) -> AsyncIterator[str | bytes]: ...


def build_provider(
    name: str,
    clock: Clock,
    *,
    url: str = RTDS_URL,
    backoff: BackoffPolicy | None = None,
    logger: logging.Logger | None = None,
    chainlink_api_key: str = "",
    chainlink_api_secret: str = "",
    chainlink_feed_id: str = "",
    chainlink_decimals: int = 0,
    chainlink_ws_url: str = "",
    symbol: str = "",
) -> TwapProvider:
    """The configured provider. Exactly one, or a fatal configuration error.

    Returns a single object and has no path that returns two, no path that holds a
    second connection open, and no fallback. An operator who configured Chainlink
    and silently got RTDS would be trading a different price source than the one
    the dashboard names, and nothing anywhere would say so.

    `url` and `backoff` come from configuration rather than from the module
    constants so that no endpoint or retry cadence is reachable only by editing
    source: a hardcoded relay address is an address the operator cannot move when
    the vendor moves it.
    """
    try:
        provider = ProviderName(name.strip().upper())
    except ValueError as exc:
        raise ConfigInvariantError(
            f"TWAP_PROVIDER must be one of {sorted(p.value for p in ProviderName)}, "
            f"got {name!r}"
        ) from exc

    if provider is ProviderName.CHAINLINK:
        return build_chainlink_feed(
            clock,
            api_key=chainlink_api_key,
            api_secret=chainlink_api_secret,
            feed_id=chainlink_feed_id,
            decimals=chainlink_decimals,
            symbol=symbol,
            base_url=chainlink_ws_url,
            backoff=backoff,
            logger=logger,
        )

    return RtdsFeed(clock, url=url, backoff=backoff, logger=logger)
