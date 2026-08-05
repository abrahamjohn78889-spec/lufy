"""Price-stream provider selection. The rest of ARC never learns which one is live.

One interface, chosen by configuration alone. The PTB Engine, Window Engine, Decision
Engine, Risk Engine, Limit Order Engine and dashboard all receive observations that
carry no trace of their origin, so swapping providers changes no strategy code and no
strategy behaviour: only the source of the price data moves.

RTDS is the only implemented provider. Chainlink is configuration-ready and nothing
more — the credentials and feed identifier have places to live, and selecting it is a
fatal configuration error until an official feed ID, official credentials and official
documentation have all been verified. There is deliberately no Chainlink module, no
placeholder feed ID and no speculative endpoint: a stub that connected to a guessed
identifier would produce prices that look exactly like real ones.

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
from arc.market.feed import RtdsFeed

__all__ = ["ProviderName", "TwapProvider", "build_provider"]


class ProviderName(StrEnum):
    """The providers that exist as configuration values.

    CHAINLINK is a member because the operator must be able to name it and get a
    clear refusal. Omitting it would produce "unknown provider CHAINLINK", which
    reads as a typo rather than as "implemented once the official details exist".
    """

    RTDS = "RTDS"
    CHAINLINK = "CHAINLINK"


@runtime_checkable
class TwapProvider(Protocol):
    """A live price stream. Yields raw frames; validates nothing."""

    @property
    def url(self) -> str: ...

    @property
    def connect_attempts(self) -> int: ...

    def messages(self) -> AsyncIterator[str | bytes]: ...


def build_provider(
    name: str,
    clock: Clock,
    *,
    logger: logging.Logger | None = None,
) -> TwapProvider:
    """The configured provider, or a fatal configuration error.

    Refuses rather than silently falling back to RTDS. An operator who configured
    Chainlink and got RTDS anyway would be trading a different price source than the
    one they believe is live, and nothing on the dashboard would say so.
    """
    try:
        provider = ProviderName(name.strip().upper())
    except ValueError as exc:
        raise ConfigInvariantError(
            f"TWAP_PROVIDER must be one of {sorted(p.value for p in ProviderName)}, "
            f"got {name!r}"
        ) from exc

    if provider is ProviderName.CHAINLINK:
        raise ConfigInvariantError(
            "TWAP_PROVIDER=CHAINLINK is not implemented. It requires a verified feed "
            "ID, verified credentials and official documentation, none of which are "
            "available; implementing it against a guessed identifier would produce "
            "prices indistinguishable from real ones. Use TWAP_PROVIDER=RTDS."
        )

    return RtdsFeed(clock, logger=logger)
