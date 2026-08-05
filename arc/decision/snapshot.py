"""Reading a fired window's frozen state, once, into an immutable snapshot.

The whole decision — validation, every risk gate, the strategy call, the intent —
runs against ONE snapshot taken at the top. Nothing downstream reads the window or
the market again.

The failure this prevents is the subtle one. The signal TWAP advances with every
arriving observation, and observations arrive on the same path that drives windows.
If gate 3 read `market.signal_twap` and the intent construction read it again, the
two could differ, and the persisted intent would record a TWAP that no gate ever
evaluated. Reading once makes the decision a function of a fixed input.

Nothing here recomputes. `direction` and `locked_trigger` are copied out of the
window verbatim; there is deliberately no arithmetic in this module that could
re-derive either from the buffer and the opening TWAP.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from arc.domain.enums import Direction, WindowState
from arc.domain.models import ExecutionWindow, MarketInstance

__all__ = ["DecisionSnapshot", "snapshot_for"]


@dataclass(frozen=True, slots=True)
class DecisionSnapshot:
    """One window's complete frozen state, plus the market values it needs.

    Every field is non-optional. A snapshot exists only when the window is fully
    frozen and the market has a PTB and a TWAP, so downstream code has no
    `is not None` branches in which a missing value could be defaulted to zero and
    silently trade against it.
    """

    market_slug: str
    close_ts: int
    offset_seconds: int
    direction: Direction
    opening_twap: Decimal
    ptb: Decimal
    buffer: Decimal
    locked_trigger: Decimal
    signal_twap: Decimal
    state: WindowState
    frozen_at: float


def snapshot_for(market: MarketInstance, window: ExecutionWindow) -> DecisionSnapshot | None:
    """Read one window's frozen state. None when anything required is absent.

    None rather than a partial object or a raise: a window that never froze is the
    ordinary case for four windows out of five on most markets, and treating it as
    an error would fill the log with exceptions for normal behaviour. The caller
    turns None into a SkipReason.
    """
    if not window.is_frozen:
        return None
    twap = market.signal_twap
    ptb = market.ptb
    if (
        twap is None
        or ptb is None
        or window.direction is None
        or window.locked_trigger is None
        or window.opening_twap is None
        or window.buffer is None
        or window.frozen_at is None
    ):
        return None
    return DecisionSnapshot(
        market_slug=market.slug,
        close_ts=market.close_ts,
        offset_seconds=window.offset_seconds,
        # Copied, never derived. See the module docstring.
        direction=window.direction,
        opening_twap=window.opening_twap,
        ptb=window.ptb if window.ptb is not None else ptb,
        buffer=window.buffer,
        locked_trigger=window.locked_trigger,
        signal_twap=twap,
        state=window.state,
        frozen_at=window.frozen_at,
    )
