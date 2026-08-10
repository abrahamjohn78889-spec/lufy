"""Engine-qualified identity for MAJORITY records.

Two engines now write to one `orders` table and one `intents` table. Every derived
identifier in the codebase was a pure function of (market, offset, ...) with no
engine component, so TWAP and MAJORITY acting on the same market and the same
window produced BYTE-IDENTICAL ids. That is not a cosmetic collision:

  * `Submitter._existing()` looks a computed order id up in the store and returns
    the row it finds. It would have returned the OTHER engine's order and reported
    the submission as already done.
  * `save_order` upserts on order_id, so the second engine would have overwritten
    the first engine's row.
  * `save_intent` has UNIQUE(market_slug, offset_seconds), so the second engine's
    intent was rejected as a duplicate of the first engine's.

The fix is a prefix, and the prefix is EMPTY FOR TWAP. That asymmetry is the whole
design: every historical TWAP id, trace id and reprice chain id keeps its exact
text, so no migration rewrites identity and no determinism assertion over old data
changes. MAJORITY gets its own namespace, which by construction cannot collide with
an id that has no prefix.

    TWAP      btc-updown-5m-1786070100:30:0:0
    MAJORITY  MAJORITY:btc-updown-5m-1786070100:30:0:0

`next_generation_id` in arc/execution/orders.py advances an id by splitting on the
LAST colon, so a prefixed id advances correctly with no change to that function.
"""

from __future__ import annotations

import hashlib
from typing import Final

from arc.majority.config import MAJORITY_ENGINE

__all__ = [
    "ENGINE_TWAP",
    "engine_prefix",
    "majority_intent_id_for",
    "majority_trace_id_for",
    "majority_window_label",
]

# The engine name carried by every pre-existing row. Not a new concept: every order,
# intent, fill and settlement written before this package existed was TWAP's, which
# is what makes the migration's DEFAULT 'TWAP' backfill a statement of fact rather
# than an assumption.
ENGINE_TWAP: Final[str] = "TWAP"

# Length of the hex trace id. Same as the TWAP derivation in arc/decision/intent.py,
# so one column width holds both and a log reader cannot tell from the shape of a
# trace id which engine emitted it — only from the engine field, which is explicit.
_TRACE_LENGTH: Final[int] = 24


def engine_prefix(engine: str) -> str:
    """The identity prefix for `engine`. EMPTY for TWAP, `"<ENGINE>:"` otherwise.

    The empty return for TWAP is load-bearing and must never become `"TWAP:"`. Every
    order id, reprice chain id and trace id already persisted was derived without a
    prefix; adding one would change the id of every live order at the next restart,
    and reconciliation matches the venue by client order id — so ARC would stop
    recognising its own resting orders and report every one of them as an orphan.
    """
    return "" if engine == ENGINE_TWAP else f"{engine}:"


def majority_intent_id_for(market_slug: str, execution_window_seconds: int) -> str:
    """`MAJORITY:{slug}:{window}`. Exactly one MAJORITY intent per market/window.

    Pure, so a post-crash replay recomputes the same id and resolves to the row
    already written instead of creating a second intent for the same decision.

    Multi-window: the window in the id is the OFFSET, not a window index, so the
    same market with windows 3 and 90 produces two distinct intents. The
    `engine, market_slug, offset_seconds` UNIQUE on the intents table is the
    arbiter, and that constraint is what enforces "one intent per window" — not
    anything in the id string. The id is purely a name.
    """
    return f"{MAJORITY_ENGINE}:{market_slug}:{execution_window_seconds}"


def majority_trace_id_for(market_slug: str, execution_window_seconds: int) -> str:
    """The correlation id threaded through every record this decision produces.

    Hashed from the engine-qualified string, so a MAJORITY trace can never equal the
    TWAP trace for the same market and window even though both are sha256 prefixes
    of the same length. Derived rather than random for the same reason the intent id
    is: a replay must correlate to the original decision, and a uuid would not.

    Two windows on the same market trace to two different ids because the window
    is in the seed.
    """
    seed = f"{MAJORITY_ENGINE}:{market_slug}:{execution_window_seconds}".encode()
    return hashlib.sha256(seed).hexdigest()[:_TRACE_LENGTH]


def majority_window_label(offset_seconds: int) -> str:
    """The operator-facing window label, e.g. `45s`.

    Used by the OPS Deck to render windows consistently in tables, dropdowns and
    log lines. Kept here so a label change (e.g. `45` → `45s`) cannot drift
    between panels.
    """
    return f"{offset_seconds}s"
