"""The twelve REST routes. Nothing else, ever.

CONTROL     POST /start  /pause  /resume  /stop
STATE       GET  /status   GET|POST /settings   GET /history
STRATEGY    GET  /strategies  /strategies/{id}   GET|POST /strategies/{id}/config
RESEARCH    GET  /backtest  /orderbook

There is no /health: PM2 restarts the process and nothing external polls it, so a
health route would be an endpoint whose only reader is a scanner. There are no
debug, admin, backup, export or diagnostics routes either — everything an operator
needs is a query parameter on one of these twelve, which is why `/history` takes
`?q=` and `?format=csv` and `/settings` takes `?snapshot=`. A route that exists but
is not in this list is a route no test guards.

Two paths accept a write as well as a read. That is one path, one resource, two
verbs — not a thirteenth route.
"""

from __future__ import annotations

import csv
import io
import shutil
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, HTTPException, Query, Request, Response, WebSocket

from arc.api import ws as ws_module
from arc.api.models import ledger_payload, status_payload, strategy_payload
from arc.config import build_trading_config, load_settings
from arc.domain.enums import Direction
from arc.domain.money import dec_str
from arc.domain.timing import MARKET_DURATION_SECONDS, window_ts_for
from arc.errors import ArcError, ArcFatalError
from arc.notify.telegram import CATEGORIES, notification_values
from arc.runtime.ledger import ledger_records, search_records
from arc.strategy.registry import default_registry

if TYPE_CHECKING:  # pragma: no cover - annotations only
    from arc.runtime.engine import ArcRuntime

__all__ = ["ROUTE_PATHS", "router"]

# The contract, as data, so a test can compare against it rather than against a
# copy of the same list written twice.
ROUTE_PATHS: tuple[str, ...] = (
    "/start",
    "/pause",
    "/resume",
    "/stop",
    "/status",
    "/settings",
    "/history",
    "/strategies",
    "/strategies/{strategy_id}",
    "/strategies/{strategy_id}/config",
    "/backtest",
    "/orderbook",
)

router = APIRouter()

_EDITABLE = frozenset(
    {"buffers", "execution_windows", "submission_count", "position_notional_usd"}
)


def _runtime(request: Request) -> ArcRuntime:
    run: ArcRuntime | None = getattr(request.app.state, "runtime", None)
    if run is None:  # pragma: no cover - the app is never mounted without a runtime
        raise HTTPException(status_code=503, detail="runtime not attached")
    return run


# ── control ──────────────────────────────────────────────────────────────────


@router.post("/start")
async def start(request: Request) -> dict[str, Any]:
    """START TRADING. Arms the operator gate; it does not start the runtime.

    The runtime is already running — `arc run` started it. This flips the one flag
    that lets an ExecutionIntent become an order, and it can never override the
    system gate: if `trading_enabled` is FALSE this returns the refusal instead of
    arming, because an operator who sees "armed" while the system has trading
    disabled would believe orders are going out.
    """
    run = _runtime(request)
    if not run.state.trading_enabled:
        raise HTTPException(
            status_code=409,
            detail=f"Trading Disabled by System / Reason: {run.state.reason}",
        )
    run.resume()
    run.arm()
    return {"execution_armed": run.state.execution_armed, "paused": run.paused}


@router.post("/pause")
async def pause(request: Request) -> dict[str, Any]:
    """Hold new submissions. Feeds, TWAP, PTB, websocket and recovery keep running."""
    run = _runtime(request)
    run.pause()
    return {"execution_armed": run.state.execution_armed, "paused": run.paused}


@router.post("/resume")
async def resume(request: Request) -> dict[str, Any]:
    run = _runtime(request)
    run.resume()
    return {"execution_armed": run.state.execution_armed, "paused": run.paused}


@router.post("/stop")
async def stop(request: Request) -> dict[str, Any]:
    """STOP TRADING. Disarms only.

    It does not cancel filled positions, stop feeds, stop the websocket, stop the
    TWAP, stop PTB observation or stop recovery. Stopping trading and stopping the
    runtime are different acts, and conflating them would mean an operator pausing
    trading also blinded the dashboard that tells them why.
    """
    run = _runtime(request)
    run.disarm()
    return {"execution_armed": run.state.execution_armed, "paused": run.paused}


# ── state ────────────────────────────────────────────────────────────────────


@router.get("/status")
async def status(request: Request) -> dict[str, Any]:
    """The whole runtime document. The same one the websocket pushes.

    One function serves both so a REST read and a socket frame can never describe
    different states — a dashboard that reconciled two shapes would have two ideas
    of which market is open.
    """
    run = _runtime(request)
    return await status_payload(run, run.clock.now())


@router.get("/settings")
async def get_settings(
    request: Request,
    snapshot: str = Query("", description="list | a snapshot name to read"),
) -> dict[str, Any]:
    """Read configuration, or the configuration snapshots, or the SQLite backup list."""
    run = _runtime(request)
    from arc.api.models import settings_payload

    if snapshot == "list":
        return {"snapshots": _snapshot_list(run)}
    if snapshot:
        stored = run.store.load_settings()
        return {"snapshot": snapshot, "values": stored}
    return settings_payload(run)


@router.post("/settings")
async def post_settings(
    request: Request,
    action: str = Query("save", description="save | backup | notifications"),
) -> dict[str, Any]:
    """Write configuration, or take a local SQLite backup.

    Refused while `execution_armed` is TRUE. A buffer edited under a resting order
    would leave that order enforcing a rule that no longer exists, so the workflow
    is Stop Trading -> Edit -> Save -> Start Trading and the lock is what makes it
    the only workflow.
    """
    run = _runtime(request)

    if action == "notifications":
        # Deliberately BEFORE the armed check. The Configuration Lock covers buffers,
        # windows, submission count and position size — things a resting order was
        # approved under. A notification toggle changes nothing about execution, and
        # locking it would mean the operator cannot silence a noisy category during
        # the only period when it is actually firing.
        return _notifications(run, await request.json())

    if run.state.execution_armed:
        raise HTTPException(
            status_code=409, detail="configuration is locked while trading is armed"
        )

    if action == "backup":
        return _backup(run)

    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="expected a JSON object")
    unknown = set(body) - _EDITABLE
    if unknown:
        raise HTTPException(
            status_code=400,
            detail=f"not editable from the dashboard: {sorted(unknown)}",
        )

    merged = run.store.load_settings() or run.settings.trading.as_storage_dict()
    merged.update({k: str(v) for k, v in body.items()})
    try:
        # Validated through the same builder the runtime boots with, so a value the
        # dashboard accepts cannot be a value that refuses to start on next launch.
        build_trading_config(merged)
        load_settings(run.settings.env, merged)
    except (ArcError, ArcFatalError) as exc:
        # ConfigInvariantError is fatal at STARTUP, but a rejected dashboard edit is
        # not fatal to a running process — the old configuration is still in force.
        # Letting it propagate would take down the runtime over a typo in a text box.
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    run.store.save_settings(merged, run.clock.now())
    return {
        "saved": True,
        # Stated plainly rather than implied: the frozen TradingConfig is fanned out
        # into seven engines at construction, and pretending an edit reached all of
        # them would leave the operator trusting a buffer that is not in force.
        "restart_required": True,
        "values": merged,
    }


@router.get("/history", response_model=None)
async def history(
    request: Request,
    q: str = Query("", description="free text over the Unified Ledger"),
    direction: str = Query(""),
    state: str = Query(""),
    result: str = Query(""),
    since: float | None = Query(None),
    until: float | None = Query(None),
    markets: int = Query(50, ge=1, le=1000),
    fmt: str = Query("json", alias="format", description="json | csv"),
) -> Response | dict[str, Any]:
    """The Unified Ledger. The only history there is.

    There is no Orders page and no Trade History page, so every filter and the CSV
    export are parameters here. A second history endpoint would be a second answer
    to "did that order fill".
    """
    run = _runtime(request)
    records = search_records(
        ledger_records(run.store, market_limit=markets),
        q,
        direction=direction,
        state=state,
        result=result,
        since=since,
        until=until,
    )
    rows = [r.as_json() for r in records]
    if fmt == "csv":
        return _csv(rows)
    payload = ledger_payload(run, market_limit=markets)
    payload["records"] = rows
    return payload


# ── strategy ─────────────────────────────────────────────────────────────────


@router.get("/strategies")
async def strategies(request: Request) -> list[dict[str, Any]]:
    return strategy_payload(_runtime(request))


@router.get("/strategies/{strategy_id}")
async def strategy(request: Request, strategy_id: str) -> dict[str, Any]:
    for entry in strategy_payload(_runtime(request)):
        if entry["id"] == strategy_id:
            return entry
    raise HTTPException(status_code=404, detail=f"no strategy {strategy_id}")


@router.get("/strategies/{strategy_id}/config")
async def strategy_config(request: Request, strategy_id: str) -> dict[str, Any]:
    """The active strategy's parameters, read-only in the dashboard.

    Displayed rather than editable: there is one strategy, it is pinned and not
    disableable, and a selector with one entry invites the belief that others exist.
    """
    run = _runtime(request)
    registry = default_registry()
    if strategy_id not in registry.ids():
        raise HTTPException(status_code=404, detail=f"no strategy {strategy_id}")
    trading = run.settings.trading
    return {
        "id": strategy_id,
        "editable": False,
        "pinned": registry.is_pinned(strategy_id),
        "config": {
            "buffers": {
                str(o): dec_str(trading.buffer_for(o)) for o in trading.windows_by_priority
            },
            "execution_windows": list(trading.windows_by_priority),
            "position_notional_usd": dec_str(trading.position_notional_usd),
            "tick_size": dec_str(trading.tick_size),
            "min_tradable_size": dec_str(trading.min_tradable_size),
            "entry_price_min": dec_str(trading.entry_price_min),
            "entry_price_max": dec_str(trading.entry_price_max),
        },
    }


@router.post("/strategies/{strategy_id}/config")
async def set_strategy_config(request: Request, strategy_id: str) -> dict[str, Any]:
    """Refused, always. The strategy is pinned (A17) and its parameters live in Settings."""
    raise HTTPException(
        status_code=405,
        detail=(
            f"{strategy_id} is pinned and not configurable from the dashboard; "
            "edit buffers and windows on the Settings page"
        ),
    )


# ── research ─────────────────────────────────────────────────────────────────


@router.get("/backtest")
async def backtest(
    request: Request,
    start: int = Query(..., description="unix seconds, inclusive"),
    end: int = Query(..., description="unix seconds, exclusive"),
) -> dict[str, Any]:
    """Signal visualisation over cached candles. Not a backtester (A18).

    No win rate, no return, no drawdown, no Sharpe, no equity curve, no optimizer.
    Polymarket settles on a 30-second TWAP and these are 5-minute spot candles, so
    any performance number computed here would be a confident answer to a question
    the data cannot answer. The warning ships in the payload so the frontend cannot
    render the chart without it.
    """
    run = _runtime(request)
    if end <= start:
        raise HTTPException(status_code=400, detail="end must be after start")
    candles = [
        {
            "open_ts": int(row["open_ts"]),
            "open": str(row["open"]),
            "high": str(row["high"]),
            "low": str(row["low"]),
            "close": str(row["close"]),
            "volume": str(row["volume"]),
        }
        for row in run.store.candles_between(start, end)
    ]
    return {
        "warning": (
            "Signal visualization only. Not performance. Polymarket settles on a "
            "30-second TWAP. Spot candles cannot reproduce settlement outcomes."
        ),
        "candles": candles,
        "markets": _replay(run, candles),
    }


@router.get("/orderbook")
async def orderbook(
    request: Request,
    direction: str = Query("UP"),
) -> dict[str, Any]:
    """The book the Limit Order Engine already uses. It decides nothing.

    Read through the executor rather than through a second venue call so the price
    shown is the price a submission would join. A separate read could disagree with
    the engine's, and the operator would be watching a book nobody trades on.
    """
    run = _runtime(request)
    market = run.rotator.current
    try:
        side = Direction(direction.upper())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"no direction {direction}") from exc
    if market is None:
        return {"market": "", "direction": side.value, "best_bid": None, "passive_limit": None}
    try:
        best = await run.executor.best_price(market.slug, side)
    except ArcError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {
        "market": market.slug,
        "direction": side.value,
        "best_bid": None if best is None else dec_str(best),
        "passive_limit": None if best is None else dec_str(best),
        "tick_size": dec_str(run.settings.trading.tick_size),
    }


# ── live ─────────────────────────────────────────────────────────────────────


@router.websocket("/ws")
async def websocket(socket: WebSocket) -> None:
    run: ArcRuntime | None = getattr(socket.app.state, "runtime", None)
    if run is None:  # pragma: no cover - the app is never mounted without a runtime
        await socket.close(code=1011)
        return
    await ws_module.serve(socket, run)


# ── helpers ──────────────────────────────────────────────────────────────────


def _csv(rows: list[dict[str, Any]]) -> Response:
    """CSV only, and every cell already a string.

    The ledger's Decimals arrive here as strings, so writing them verbatim is what
    keeps a spreadsheet from reading 0.740000 as 0.74 and back as a float.
    """
    buffer = io.StringIO()
    fields = list(rows[0]) if rows else ["market"]
    writer = csv.DictWriter(buffer, fieldnames=fields, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)
    return Response(content=buffer.getvalue(), media_type="text/csv")


def _snapshot_list(run: ArcRuntime) -> list[dict[str, Any]]:
    directory = run.store.path.parent
    return [
        {"name": p.name, "bytes": p.stat().st_size, "modified": p.stat().st_mtime}
        for p in sorted(directory.glob(f"{run.store.path.stem}-*.db"))
    ]


def _notifications(run: ArcRuntime, body: Any) -> dict[str, Any]:
    """Apply the twenty-one toggles, in memory and on disk.

    The notifier holds its flag dict BY REFERENCE, so mutating it in place is what
    makes a toggle take effect without a restart. Rebinding a new dict here would
    persist correctly and change nothing about what actually gets sent.
    """
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="expected a JSON object")
    unknown = set(body) - set(CATEGORIES)
    if unknown:
        raise HTTPException(
            status_code=400, detail=f"unknown notification categories: {sorted(unknown)}"
        )
    flags = run.notifier.flags
    flags.update({name: bool(value) for name, value in body.items()})

    stored = run.store.load_settings() or run.settings.trading.as_storage_dict()
    stored.update(notification_values(flags))
    run.store.save_settings(stored, run.clock.now())
    return {"saved": True, "notifications": dict(flags)}


def _backup(run: ArcRuntime) -> dict[str, Any]:
    """Timestamped local copy. Local only; nothing is uploaded anywhere."""
    stamp = int(run.clock.now())
    target = run.store.path.with_name(f"{run.store.path.stem}-{stamp}.db")
    # copy2 rather than the SQLite backup API: the connection is WAL-mode and open,
    # and a file copy of the live database plus its WAL is what the operator can
    # actually restore by hand on a VPS with no tooling installed.
    run.store.checkpoint()
    shutil.copy2(run.store.path, target)
    return {"backup": target.name, "bytes": target.stat().st_size, "path": str(target)}


def _replay(run: ArcRuntime, candles: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Replay ARC's own trigger maths over the candles, per market.

    The signal TWAP here is the cumulative mean of candle closes inside the market,
    which is ARC's own definition applied to the only history that exists. It is
    labelled a replay and never a fill: a 5-minute close is not a 300-sample mean,
    and treating the two as the same number is exactly what the warning is about.
    """
    trading = run.settings.trading
    by_market: dict[int, list[dict[str, Any]]] = {}
    for candle in candles:
        by_market.setdefault(window_ts_for(int(candle["open_ts"])), []).append(candle)

    out: list[dict[str, Any]] = []
    for window_ts, group in sorted(by_market.items()):
        closes = [Decimal(c["close"]) for c in group]
        if not closes:
            continue
        ptb = closes[0]
        cumulative = sum(closes, Decimal("0")) / Decimal(len(closes))
        windows = []
        for offset in trading.windows_by_priority:
            buffer_ = trading.buffer_for(offset)
            up = ptb + buffer_
            down = ptb - buffer_
            windows.append(
                {
                    "offset_seconds": offset,
                    "buffer": dec_str(buffer_),
                    "trigger_up": dec_str(up),
                    "trigger_down": dec_str(down),
                    "fired": cumulative >= up or cumulative <= down,
                }
            )
        out.append(
            {
                "window_ts": window_ts,
                "close_ts": window_ts + MARKET_DURATION_SECONDS,
                "ptb": dec_str(ptb),
                "signal_twap": dec_str(cumulative),
                "windows": windows,
            }
        )
    return out
