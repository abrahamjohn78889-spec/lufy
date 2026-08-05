"""The strategy plugin protocol: three frozen records and one pure method.

Everything a strategy may see arrives in StrategyContext. Everything it may say
leaves in StrategyDecision. There is no third channel — no store, no logger, no
clock, no callback — because any of those would let a strategy act rather than
advise, and the Risk Engine is the only thing permitted to gate an action.

The context deliberately carries values that are ALREADY FROZEN rather than the
market object they came from. Handing a strategy the live MarketInstance would let
it read `market.signal_twap` a second time and get a different number than the one
the window froze against, so the same decision could come out differently
depending on when inside the pass it looked.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol, runtime_checkable

from arc.domain.enums import Direction

__all__ = [
    "Strategy",
    "StrategyContext",
    "StrategyDecision",
    "StrategyDescription",
]


@dataclass(frozen=True, slots=True)
class StrategyContext:
    """Everything a strategy is allowed to see. Immutable, and already frozen.

    `signal_twap` is ARC's own 300-second cumulative mean — the STRATEGY INPUT.
    The venue's 30-second settlement TWAP is deliberately absent: it is the
    outcome quantity and feeds no decision in any phase (A6). A strategy that
    could see it would be fitting to the answer.
    """

    market_slug: str
    close_ts: int
    offset_seconds: int
    direction: Direction
    opening_twap: Decimal
    ptb: Decimal
    buffer: Decimal
    locked_trigger: Decimal
    signal_twap: Decimal
    # Book side price the order would cross at, quoted for `direction`. Supplied by
    # the caller so the strategy performs no I/O of its own.
    quote_price: Decimal
    # Budget and venue arithmetic, passed in rather than read from config, so the
    # strategy holds no configuration reference it could mutate or re-read.
    position_notional_usd: Decimal
    tick_size: Decimal
    min_tradable_size: Decimal


@dataclass(frozen=True, slots=True)
class StrategyDecision:
    """What a strategy proposes. A PROPOSAL, never an authorisation.

    `act=False` is an ordinary outcome, not an error: most windows never cross.
    `reason` explains a refusal in plain words for the log; it is not a
    DenialReason, because a strategy declining to act is not a risk denial and
    conflating the two would make the rejection log claim a gate fired when none
    did.
    """

    act: bool
    limit_price: Decimal
    size: Decimal
    reason: str = ""


@dataclass(frozen=True, slots=True)
class StrategyDescription:
    """What the API and Settings page display for a strategy.

    `pinned` and `disableable` are carried on the description rather than kept in
    the registry so that "the default cannot be turned off" travels with the
    strategy itself and cannot be lost by a registry rewrite (A17).
    """

    strategy_id: str
    name: str
    description: str
    pinned: bool = False
    disableable: bool = True


@runtime_checkable
class Strategy(Protocol):
    """A strategy is a pure function of its context, plus its own description.

    runtime_checkable so a registration can be rejected at registration time
    rather than at the first fired window, which in a five-minute market is up to
    five minutes of silent non-trading.
    """

    def describe(self) -> StrategyDescription:
        """Static metadata. Must not depend on any input."""
        ...

    def decide(self, context: StrategyContext) -> StrategyDecision:
        """Pure: same context in, same decision out, every time, forever."""
        ...
