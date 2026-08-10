"""The MAJORITY engine: a second, independent trading engine.

MAJORITY and TWAP are two engines in one process. They share infrastructure —
market discovery, the market grid, the CLOB book refresh, the Executor protocol,
the Risk Engine, the wallet, the database, reconciliation, fills, repricing,
sweeping, the event hub, Telegram, the ledger and recovery — and share nothing
else. Trigger state, selected side, order ownership, arm state and configuration
are per-engine, because a shared one is a path by which one engine's decision
becomes the other engine's order.

WHAT MAJORITY TRADES ON. Polymarket outcome-share CLOB prices, and nothing else.
Not BTC/USD, not the PTB, not the signal TWAP, not the settlement TWAP, not the
last trade, not the midpoint. The shares are probabilities in 0.00..1.00 and the
only field read is the best resting bid on each side.

WHY IT IS NOT A STRATEGY. TWAP's `Strategy` protocol receives `direction` as an
INPUT: direction is frozen when the window activates, and the strategy only sizes.
MAJORITY determines its side at the TRIGGER instant, which that protocol
structurally cannot express. So MAJORITY is an engine beside the Decision Engine
rather than a plugin inside it, and `Strategy.decide()` is never on its path.

THE TWO-STEP RULE, which is the whole point of this package:

    1. the trigger fires when  max(best_bid(UP), best_bid(DOWN)) >= trigger_price
    2. the side is determined AFTERWARDS, from a FRESH book read

The side that crossed the trigger is NOT the side that gets bought. A trigger is
an instruction to go and look, not an answer.
"""

from __future__ import annotations

__all__: tuple[str, ...] = ()
