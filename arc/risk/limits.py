"""The risk limits, projected out of the validated trading configuration.

Separate from StrategyConfig on purpose. A strategy must not be able to see the
position limit, the loss limits or the opposing-direction policy: a strategy that
could read them could shape its proposal to slip past a gate, and the gates would
then be measuring a decision that was made with the gates in mind.

Nothing here has a default. Every value is an operator setting (A17), and a risk
limit with a code default is a number nobody chose that decides whether real money
is committed.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from arc.config import TradingConfig

__all__ = ["RiskLimits", "limits_from_trading"]


@dataclass(frozen=True, slots=True)
class RiskLimits:
    """Every configured bound the fourteen gates evaluate against."""

    max_trades_per_market: int
    max_concurrent_positions: int
    max_daily_loss_usd: Decimal
    max_consecutive_losses: int
    entry_price_min: Decimal
    entry_price_max: Decimal
    min_tradable_size: Decimal
    allow_opposing_directions: bool


def limits_from_trading(trading: TradingConfig) -> RiskLimits:
    """Project the validated trading configuration into the risk layer's view."""
    return RiskLimits(
        max_trades_per_market=trading.max_trades_per_market,
        max_concurrent_positions=trading.max_concurrent_positions,
        max_daily_loss_usd=trading.max_daily_loss_usd,
        max_consecutive_losses=trading.max_consecutive_losses,
        entry_price_min=trading.entry_price_min,
        entry_price_max=trading.entry_price_max,
        min_tradable_size=trading.min_tradable_size,
        allow_opposing_directions=trading.allow_opposing_directions,
    )
