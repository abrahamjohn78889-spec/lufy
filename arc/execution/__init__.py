"""The Limit Order Engine.

This package places, monitors, cancels and reconciles orders. It knows nothing about
why an order exists. Its only input is the immutable ExecutionIntent, which it
consumes verbatim: no value is recalculated, no decision is revalidated, no market
state is re-read. Every module here greps clean of the decision layer's vocabulary
(A17), which is what makes that boundary a structural property rather than a
convention someone has to remember.

No module here reads a clock (A10/D1). `now` is always a parameter. The one execution
boundary that exists is the market phase: submissions are refused once the market is
CANCELLING, and nothing anywhere asks whether a window is "too late".
"""

from __future__ import annotations

__all__: tuple[str, ...] = ()
