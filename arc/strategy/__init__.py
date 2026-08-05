"""The strategy layer: what to trade, expressed as a pure function.

A strategy here is a PURE FUNCTION (A17). It receives an immutable
StrategyContext of already-frozen numbers and returns an immutable
StrategyDecision. It has no clock, no socket, no database handle and no store; it
cannot place, cancel, reprice or size an order at the venue, and it cannot reach
past the Risk Engine.

That restriction is the whole design. A strategy that could read a clock would
produce different answers on replay, and a strategy that could submit would make
the risk gates optional. Both failures are invisible in testing and expensive in
production, so the type signature removes them rather than a convention.

Exactly one strategy is registered: `arc_twap_locked_buffer`. The registry exists
as the architectural boundary for a second one, not as a place to park stubs —
additional strategies are gated behind 100+ real markets of V1 data (A17).
"""

from arc.strategy.arc_twap_locked_buffer import ArcTwapLockedBuffer
from arc.strategy.config import StrategyConfig, config_from_trading
from arc.strategy.presets import PRESETS, Preset, preset_values
from arc.strategy.protocol import (
    Strategy,
    StrategyContext,
    StrategyDecision,
    StrategyDescription,
)
from arc.strategy.registry import (
    DEFAULT_STRATEGY_ID,
    StrategyRegistry,
    default_registry,
)

__all__ = [
    "DEFAULT_STRATEGY_ID",
    "PRESETS",
    "ArcTwapLockedBuffer",
    "Preset",
    "Strategy",
    "StrategyConfig",
    "StrategyContext",
    "StrategyDecision",
    "StrategyDescription",
    "StrategyRegistry",
    "config_from_trading",
    "default_registry",
    "preset_values",
]
