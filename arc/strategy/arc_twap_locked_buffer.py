"""ARC TWAP + Locked Buffer — the one implemented strategy (A17).

The window has already done the hard part. By the time this is consulted, the
window froze five values atomically and its locked trigger has been satisfied:
UP fires when `signal_twap >= locked_trigger`, DOWN when
`signal_twap <= locked_trigger`. Neither of those comparisons is repeated here.
Re-deriving direction, trigger or buffer would reintroduce exactly the defect that
verbatim restore exists to prevent — the frozen numbers and the live ones differ,
and a recomputed decision looks entirely healthy while trading a trigger nobody
configured.

So what is left is sizing: turn a satisfied trigger into a limit price and a share
count. That is all this does, and it does it with exact Decimal arithmetic and
ROUND_FLOOR, so the notional can only ever come out at or under budget.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Final

from arc.domain.money import quantize_price, shares_for_notional
from arc.strategy.protocol import (
    StrategyContext,
    StrategyDecision,
    StrategyDescription,
)

__all__ = ["STRATEGY_ID", "ArcTwapLockedBuffer"]

STRATEGY_ID: Final[str] = "arc_twap_locked_buffer"

_ZERO: Final[Decimal] = Decimal("0")


class ArcTwapLockedBuffer:
    """The default strategy. Stateless, and therefore trivially pure.

    __slots__ = () rather than a plain class body: an instance with no attribute
    storage cannot accumulate per-market state, so "this strategy remembers
    nothing between markets" is a property of the object layout rather than of the
    code remembering not to assign (A11).
    """

    __slots__ = ()

    def describe(self) -> StrategyDescription:
        return StrategyDescription(
            strategy_id=STRATEGY_ID,
            name="ARC TWAP + Locked Buffer",
            description=(
                "Freezes the opening 300s signal TWAP, the official PTB and the "
                "configured buffer at window activation, then acts when the signal "
                "TWAP crosses the locked trigger in the frozen direction."
            ),
            pinned=True,
            disableable=False,
        )

    def decide(self, context: StrategyContext) -> StrategyDecision:
        """Size the trade the window has already authorised.

        No clock read, no I/O, no randomness, and no use of the venue's settlement
        TWAP — so a replay of the same context produces the same decision on any
        machine on any day.
        """
        if context.quote_price <= _ZERO:
            # A non-positive quote means no usable price was available, not a
            # free trade. Dividing the budget by it would raise or produce an
            # absurd size.
            return StrategyDecision(
                act=False,
                limit_price=_ZERO,
                size=_ZERO,
                reason=f"no usable quote for {context.direction.value}",
            )

        # Quantize BEFORE any validation or sizing (defect D2). Flooring after a
        # size has been derived from an unquantized price leaves the two
        # inconsistent, and the venue would reject the pair.
        limit_price = quantize_price(context.quote_price, context.tick_size)
        if limit_price <= _ZERO:
            # The quote was a fraction of one tick, so flooring it reaches zero.
            return StrategyDecision(
                act=False,
                limit_price=_ZERO,
                size=_ZERO,
                reason=f"quote {context.quote_price} floors to zero at tick {context.tick_size}",
            )

        # Floored division, so shares * price is at or under budget and never over
        # it by one share's worth. The step is one share: `min_tradable_size` is a
        # FLOOR, not a lot size, and passing it as the step here would quantize a
        # 19-share order down to 15 and quietly underspend the budget by a fifth.
        size = shares_for_notional(context.position_notional_usd, limit_price)
        if size < context.min_tradable_size:
            return StrategyDecision(
                act=False,
                limit_price=limit_price,
                size=size,
                reason=(
                    f"budget {context.position_notional_usd} buys {size} shares at "
                    f"{limit_price}, below the {context.min_tradable_size} minimum"
                ),
            )

        return StrategyDecision(act=True, limit_price=limit_price, size=size)
