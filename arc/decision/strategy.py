"""The adapter between a decision snapshot and the strategy protocol.

Thin on purpose. Its whole job is to hand the strategy exactly what it is allowed
to see and nothing more: no TradingConfig reference, no MarketInstance, no Store,
no clock. A strategy that held any of those would stop being a pure function of its
context, and a replay of the same observation stream could then produce a different
decision.

The venue's settlement TWAP is not passed. It is the OUTCOME quantity (A6) — a
strategy able to read it would be fitting to the answer.
"""

from __future__ import annotations

from decimal import Decimal

from arc.decision.snapshot import DecisionSnapshot
from arc.strategy.config import StrategyConfig
from arc.strategy.protocol import StrategyContext

__all__ = ["context_for"]


def context_for(
    snapshot: DecisionSnapshot,
    config: StrategyConfig,
    *,
    quote_price: Decimal,
) -> StrategyContext:
    """Project the snapshot and the strategy config into a StrategyContext.

    `quote_price` is supplied by the caller, already quoted for the frozen
    direction. The strategy performs no I/O of its own, so it cannot fetch a book,
    and cannot therefore fetch one for the wrong side.
    """
    return StrategyContext(
        market_slug=snapshot.market_slug,
        close_ts=snapshot.close_ts,
        offset_seconds=snapshot.offset_seconds,
        direction=snapshot.direction,
        opening_twap=snapshot.opening_twap,
        ptb=snapshot.ptb,
        buffer=snapshot.buffer,
        locked_trigger=snapshot.locked_trigger,
        signal_twap=snapshot.signal_twap,
        quote_price=quote_price,
        position_notional_usd=config.position_notional_usd,
        tick_size=config.tick_size,
        min_tradable_size=config.min_tradable_size,
    )
