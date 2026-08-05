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
    INCOMPLETE      frozen state is present but a required value is missing
    NO_QUOTE        no usable book price was supplied for the frozen direction
    STRATEGY_HELD   the strategy was consulted and declined
    LOWER_PRIORITY  single-trade mode: a nearer window already produced the intent
    ALREADY_DECIDED an intent for this window exists; nothing more to do
    """

    NOT_FIRED = "NOT_FIRED"
    NOT_FROZEN = "NOT_FROZEN"
    INCOMPLETE = "INCOMPLETE"
    NO_QUOTE = "NO_QUOTE"
    STRATEGY_HELD = "STRATEGY_HELD"
    LOWER_PRIORITY = "LOWER_PRIORITY"
    ALREADY_DECIDED = "ALREADY_DECIDED"
