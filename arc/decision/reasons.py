"""Why the Decision Engine produced no intent, when no risk gate refused.

Distinct from DenialReason on purpose. A DenialReason means a RISK GATE fired: the
trade was possible and policy refused it. A SkipReason means there was nothing to
decide, or the strategy itself declined to act. Collapsing the two would make the
rejection log claim a gate refused a trade when none did, and an operator tuning
buffers would go looking for a risk setting that was never involved.
"""

from __future__ import annotations

from enum import StrEnum

__all__ = ["SkipReason"]


class SkipReason(StrEnum):
    """Non-denial outcomes.

    NOT_FIRED       the window has not been triggered; the ordinary case
    NOT_FROZEN      the window never froze, so it carries no trigger to act on
    NO_DIRECTION    the frozen TWAP equalled the official PTB; no side to trade
    INCOMPLETE      frozen state is present but a required value is missing
    NO_QUOTE        no usable book price was supplied for the frozen direction
    STRATEGY_HELD   the strategy was consulted and declined
    LOWER_PRIORITY  single-trade mode: a nearer window already produced the intent
    ALREADY_DECIDED an intent for this window exists; nothing more to do

    NO_DIRECTION is distinct from NOT_FROZEN on purpose. NOT_FROZEN describes a window
    that has no trigger yet and may still get one; NO_DIRECTION is terminal and
    intentional — strict comparison found the TWAP exactly on the PTB, so the contract
    says trade neither side. An operator seeing NOT_FROZEN would go looking for a
    stalled freeze that never happened.
    """

    NOT_FIRED = "NOT_FIRED"
    NOT_FROZEN = "NOT_FROZEN"
    NO_DIRECTION = "NO_DIRECTION"
    INCOMPLETE = "INCOMPLETE"
    NO_QUOTE = "NO_QUOTE"
    STRATEGY_HELD = "STRATEGY_HELD"
    LOWER_PRIORITY = "LOWER_PRIORITY"
    ALREADY_DECIDED = "ALREADY_DECIDED"
