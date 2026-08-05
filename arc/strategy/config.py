"""Per-strategy configuration. Every value comes from the operator's config.

Nothing here has a default. A strategy parameter with a code default is a number
nobody chose that trades real money and reads, afterwards, exactly like a
deliberate setting (A17). So this is a projection of the validated TradingConfig,
not a second place where trading numbers are decided.

The projection exists so a strategy never holds the TradingConfig itself: a
strategy that could reach `trading.buffers` could read a buffer for a window other
than the one it was asked about, and would then be a function of global
configuration rather than of its context.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from arc.config import TradingConfig

__all__ = ["StrategyConfig", "config_from_trading"]


@dataclass(frozen=True, slots=True)
class StrategyConfig:
    """The subset of trading configuration a strategy is permitted to depend on."""

    execution_windows: tuple[int, ...]
    buffers: dict[int, Decimal]
    position_notional_usd: Decimal
    entry_price_min: Decimal
    entry_price_max: Decimal
    tick_size: Decimal
    min_tradable_size: Decimal

    def buffer_for(self, offset_seconds: int) -> Decimal:
        return self.buffers[offset_seconds]

    def implied_btc_move(self, offset_seconds: int) -> Decimal:
        """How far BTC must move for the 300s mean to travel one buffer (A7).

        Displayed while tuning. Kept here as well as on TradingConfig because a
        preset is only interpretable next to this number: 1.00 on the 3s window is
        a $100 move, and 1.00 on the 15s window is $20.
        """
        return self.buffers[offset_seconds] * (Decimal(300) / Decimal(offset_seconds))


def config_from_trading(trading: TradingConfig) -> StrategyConfig:
    """Project the validated trading configuration into the strategy's view.

    A copy of the buffers dict, not the original: the strategy layer must not be
    able to reach the object the engine is holding.
    """
    return StrategyConfig(
        execution_windows=trading.windows_by_priority,
        buffers=dict(trading.buffers),
        position_notional_usd=trading.position_notional_usd,
        entry_price_min=trading.entry_price_min,
        entry_price_max=trading.entry_price_max,
        tick_size=trading.tick_size,
        min_tradable_size=trading.min_tradable_size,
    )
