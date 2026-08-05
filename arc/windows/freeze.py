"""Window freeze: five values locked together, persisted, or nothing changed at all.

    opening_twap    ARC's cumulative signal mean at the activation instant
    ptb             the market's official opening reference, frozen once
    buffer          the configured buffer for THIS offset
    direction       UP if opening_twap > ptb, DOWN if opening_twap < ptb, and NEITHER
                    on equality — strict comparison only, no >= and no <=
    locked_trigger  opening_twap + buffer (UP) or opening_twap - buffer (DOWN)

EQUALITY IS NOT A FREEZE. When the frozen TWAP equals the official PTB exactly, no
direction exists, so there are no five values to lock: the window is retired to the
terminal NO_DIRECTION state and authorises nothing. It is terminal rather than left
PENDING because direction is determined once, at the opening instant, and a retried
window would freeze against a TWAP the contract says is not to be consulted.

ATOMICITY (criterion 3). Every failure mode leaves all five as None and the window
PENDING. ExecutionWindow.freeze already computes into locals and assigns in one block,
so a validation failure cannot half-write the object; this module's contribution is
that a PERSISTENCE failure cannot either. If the row will not write, the in-memory
freeze is rolled back, because the alternative is a window that is frozen in this
process and unfrozen on disk — which reloads after a restart as a window that never
froze, while the running process has already acted on its trigger.

The failure this prevents is not "a window is missing values". It is a window holding a
real opening_twap beside a defaulted buffer: a locked trigger that was never
configured, on a window that looks completely healthy from every log line and every
dashboard panel (A12).

PERSIST BEFORE EVALUATE (criterion 4). The row is written before this function returns,
and evaluation only ever runs on windows that are FROZEN. So the trigger is on disk
before any comparison against it can pass, and therefore before any intent it could
authorise. A crash in the microsecond between the freeze and the fire still leaves the
exact trigger on disk to reload verbatim (A4). synchronous=FULL is what makes that
claim true rather than likely.

ONE PTB FOR ALL FIVE WINDOWS. The PTB comes from the MarketInstance, which exposes it
as a read-only property set exactly once. There is no argument here through which a
caller could pass a different one, so "all five windows share one reference" is
structural, not a convention (A12).
"""

from __future__ import annotations

import logging
from decimal import Decimal

from arc.config import TradingConfig
from arc.domain.enums import WindowState
from arc.domain.models import ExecutionWindow, MarketInstance
from arc.errors import NoDirectionError, StorageError, WindowFreezeError
from arc.logging_setup import log_event
from arc.storage.store import Store
from arc.windows.lifecycle import no_direction_window

__all__ = ["freeze_due_window", "freeze_window", "restore_window"]


def freeze_window(
    market: MarketInstance,
    window: ExecutionWindow,
    *,
    trading: TradingConfig,
    store: Store,
    now: float,
    logger: logging.Logger | None = None,
) -> bool:
    """Freeze one window and persist it. True on success; False if already frozen.

    Returns False rather than raising for an already-frozen window because the
    activation path is level-triggered and idempotent by design: a second pass over a
    frozen window is the normal case, not an error.

    Raises WindowFreezeError when the freeze was ATTEMPTED and could not be completed —
    no PTB, no observations yet, a missing buffer, or a refused row. In every one of
    those cases the window is left exactly as it was found.

    Raises NoDirectionError, separately and deliberately, when the frozen TWAP equalled
    the official PTB. That is a final verdict rather than a retryable failure, so the
    caller must retire the window instead of leaving it for the next pass.
    """
    if window.state is not WindowState.PENDING:
        return False

    offset = window.offset_seconds

    # Read the buffer BEFORE touching the window. A missing buffer is a configuration
    # error, and config forbids defaulting one (a window with no buffer can never fire),
    # so it must surface before any state changes rather than after.
    try:
        buffer: Decimal = trading.buffer_for(offset)
    except KeyError as exc:
        raise WindowFreezeError(
            f"window {offset}s of {market.slug} has no configured buffer; "
            "a window without a buffer can never fire"
        ) from exc

    # freeze_window on the instance validates the PTB and the TWAP and performs the
    # single-assignment commit. Anything it raises leaves the window untouched.
    market.freeze_window(offset, buffer=buffer, frozen_at=now)

    try:
        persisted = store.save_window_frozen(market.slug, window, now)
    except StorageError as exc:
        _rollback(window)
        raise WindowFreezeError(
            f"window {offset}s of {market.slug} could not be persisted ({exc}); "
            "freeze rolled back — an unpersisted trigger would vanish on restart"
        ) from exc

    if not persisted:
        # No row matched. The window row is created with the market, so its absence
        # means this market was never persisted — and a frozen window with no row on
        # disk is precisely the state that reloads as "never froze" while this process
        # keeps acting on the trigger.
        _rollback(window)
        raise WindowFreezeError(
            f"window {offset}s of {market.slug} has no row to update; freeze rolled back"
        )

    log_event(
        logging.INFO,
        "Window Frozen",
        f"{market.slug}  {offset}s  {window.direction}  "
        f"twap {window.opening_twap}  ptb {window.ptb}  "
        f"buffer {window.buffer}  trigger {window.locked_trigger}",
        logger=logger,
    )
    return True


def _rollback(window: ExecutionWindow) -> None:
    """Undo an in-memory freeze that could not be persisted. All five, plus state.

    The one place in this codebase that clears frozen values. It exists only for the
    window between the in-memory commit and the durable write, and it runs only when
    the durable write failed — so no window that any caller has been told is frozen
    can be cleared by it.
    """
    window.opening_twap = None
    window.ptb = None
    window.buffer = None
    window.direction = None
    window.locked_trigger = None
    window.frozen_at = None
    window.state = WindowState.PENDING


def freeze_due_window(
    market: MarketInstance,
    window: ExecutionWindow,
    *,
    trading: TradingConfig,
    store: Store,
    now: float,
    logger: logging.Logger | None = None,
) -> bool:
    """freeze_window, with a failed freeze contained instead of propagated.

    One window's failure must not stop the others (criterion 18). A market whose 10s
    window could not freeze still has four windows with valid triggers, and raising
    through the market loop would abandon all of them.

    The failure is logged at WARNING and the window stays PENDING, so the next pass
    retries it. That is the correct behaviour for the realistic cause — no observations
    have arrived yet — which resolves by itself on the following tick.

    NoDirectionError is handled differently and must be: the window is retired to
    NO_DIRECTION and persisted. Retrying it would freeze a direction against a later
    TWAP, and direction is determined exactly once at the opening instant.
    """
    try:
        return freeze_window(
            market, window, trading=trading, store=store, now=now, logger=logger
        )
    except NoDirectionError as exc:
        if no_direction_window(window, logger=logger):
            store.save_window_state(market.slug, window.offset_seconds, window.state)
        log_event(
            logging.INFO,
            "Window No Direction",
            f"{market.slug}  {window.offset_seconds}s  {exc}",
            logger=logger,
        )
        return False
    except WindowFreezeError as exc:
        log_event(
            logging.WARNING,
            "Window Freeze Rejected",
            f"{market.slug}  {window.offset_seconds}s  {exc}",
            logger=logger,
        )
        return False


def restore_window(
    market: MarketInstance,
    offset_seconds: int,
    *,
    store: Store,
) -> bool:
    """Reload one window's frozen values from disk VERBATIM. False if never frozen.

    direction and locked_trigger are read from the row and passed through unchanged.
    Nothing here recomputes them, and this function deliberately has no access to the
    market's current signal TWAP with which it could try.

    That restraint is the entire reason the values are persisted (A4). The TWAP has
    moved since the freeze — that is what the window is watching for — so recomputing
    the trigger from the post-restart TWAP produces a DIFFERENT trigger than the window
    locked. The bot would then keep running, look perfectly healthy, and trade a
    strategy nobody configured.

    A NO_DIRECTION window is restored too, and has to be. It carries no frozen values,
    so restore_frozen returns None for it; without the state check below it would come
    back PENDING and the next pass would determine its direction a second time, against
    a TWAP that has since moved off the PTB it equalled.
    """
    state = store.window_state(market.slug, offset_seconds)
    if state is WindowState.NO_DIRECTION:
        market.window(offset_seconds).state = WindowState.NO_DIRECTION
        return True

    frozen = store.restore_frozen(market.slug, offset_seconds)
    if frozen is None:
        return False
    window = market.window(offset_seconds)
    window.restore_frozen(
        opening_twap=frozen["opening_twap"],
        ptb=frozen["ptb"],
        buffer=frozen["buffer"],
        direction=frozen["direction"],
        locked_trigger=frozen["locked_trigger"],
        frozen_at=frozen["frozen_at"],
        state=frozen["state"],
    )
    window.fired_at = frozen["fired_at"]
    return True
