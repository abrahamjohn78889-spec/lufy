"""The twelve REST routes. Nothing else, ever.

CONTROL     POST /start  /pause  /resume  /stop
STATE       GET  /status   GET|POST /settings   GET /history
STRATEGY    GET  /strategies  /strategies/{id}   GET|POST /strategies/{id}/config
RESEARCH    GET  /backtest  /orderbook

There is no /health: PM2 restarts the process and nothing external polls it, so a
health route would be an endpoint whose only reader is a scanner. There are no
debug, admin, backup, export or diagnostics routes either — everything an operator
needs is a query parameter on one of these twelve, which is why `/history` takes
`?q=`, `?format=csv`, `?format=report` and `?validate=1`, and `/settings` takes
`?snapshot=`. A route that exists but is not in this list is a route no test
guards.

Two paths accept a write as well as a read. That is one path, one resource, two
verbs — not a thirteenth route.

START/STOP ARE THE RUNTIME, NOT TRADING. `/start` boots the entire selected
runtime — providers, CLOB, websockets, discovery, recovery, engines, recorder —
and arms NOTHING. `/stop` shuts all of it down. Arming trading is a separate
operator act on `/strategies/{id}/config?action=arm`, which is the Limit Order
Engine's own control surface and already exists. Trading therefore cannot begin
as a side effect of starting the system, and the runtime can sit fully running
with trading idle, which is the normal state between windows.
"""

from __future__ import annotations

import csv
import io
import re
import shutil
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, HTTPException, Query, Request, Response, WebSocket

from arc.api import ws as ws_module
from arc.api.models import ledger_payload, preflight, status_payload, strategy_payload
from arc.config import build_trading_config
from arc.domain.enums import Direction, Mode
from arc.domain.money import dec_str
from arc.domain.timing import MARKET_DURATION_SECONDS, window_ts_for
from arc.errors import ArcError, ArcFatalError
from arc.notify.telegram import CATEGORIES, notification_values
from arc.runtime.ledger import ledger_records, search_records
from arc.runtime.report import render_report
from arc.runtime.validation import validate_run
from arc.strategy.registry import default_registry
from arc.timefmt import parse_at

if TYPE_CHECKING:  # pragma: no cover - annotations only
    from arc.runtime.engine import ArcRuntime
    from arc.runtime.supervisor import RuntimeSupervisor

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
    {
        # TWAP fields — original set
        "buffers",
        "execution_windows",
        "submission_count",
        "position_notional_usd",
        # MAJORITY fields — added so the Settings page controls both engines.
        # The save flow validates both halves: TWAP through build_trading_config,
        # MAJORITY through build_majority_config. A bad MAJORITY value is rejected
        # with a 400 before anything is written, and restart_required is True so
        # the operator knows a restart is needed after any change.
        "majority_enabled",
        "majority_buffer",
        "majority_trigger_price",
        "majority_target_limit_price",
        "majority_shares",
        "majority_entry_price_min",
        "majority_entry_price_max",
        "majority_execution_windows",
    }
)


def _runtime(request: Request) -> ArcRuntime:
    """The live runtime object.

    Read through the supervisor when there is one, because the supervisor swaps
    the runtime on every start and a reference captured at mount time would keep
    serving the stopped run's markets and accumulators to every panel.
    """
    sup: RuntimeSupervisor | None = getattr(request.app.state, "supervisor", None)
    if sup is not None:
        return sup.runtime
    run: ArcRuntime | None = getattr(request.app.state, "runtime", None)
    if run is None:  # pragma: no cover - the app is never mounted without a runtime
        raise HTTPException(status_code=503, detail="runtime not attached")
    return run


def _supervisor(request: Request) -> RuntimeSupervisor:
    """The runtime's owner. Present whenever the app was built by `arc run`.

    The runtime is read through `supervisor.runtime` rather than cached on app
    state, because the supervisor REPLACES the runtime object on every start and
    a cached reference would keep rendering the stopped one.
    """
    sup: RuntimeSupervisor | None = getattr(request.app.state, "supervisor", None)
    if sup is None:  # pragma: no cover - the app is never mounted without one
        raise HTTPException(status_code=503, detail="supervisor not attached")
    return sup


def _mode(name: str) -> Mode:
    """Parse the runtime selector. Rejects anything that is not V1 or V2.

    A 400 rather than a fallback to V1: an operator who asked for a runtime the
    system does not have must not be given a different one silently, and the one
    they would be given is the one that does not trade.
    """
    try:
        return Mode(name.strip().upper())
    except ValueError:
        raise HTTPException(
            status_code=400, detail=f"unknown runtime {name!r}; expected V1 or V2"
        ) from None


# ── control ──────────────────────────────────────────────────────────────────


@router.post("/start")
async def start(
    request: Request,
    mode: str = Query("", description="V1 | V2; blank keeps the current selection"),
) -> dict[str, Any]:
    """START RUNTIME. Brings the entire selected system up. Arms nothing.

    Providers, RTDS or Chainlink, market discovery, PTB, CLOB, the official
    websockets, decision, risk and limit order engines, Signal Tank, ledger,
    Telegram, recovery, recorder and health monitoring all start together. The
    only difference between V1 and V2 is the executor.

    V2 additionally requires preflight to PASS. The checks run against the
    runtime that is about to be replaced, so they cover configuration, wallet,
    credentials, provider, connectivity, database, runtime and recovery state
    before any live adapter exists. A FAIL refuses the start and returns the
    failing checks by name — "preflight failed" alone sends the operator to the
    logs, which is the trip this dashboard exists to remove.

    Trading does NOT begin here. `execution_armed` stays FALSE until the operator
    arms the Limit Order Engine, so a start can never be a trade.
    """
    sup = _supervisor(request)
    selected = _mode(mode) if mode else sup.mode
    if sup.running:
        raise HTTPException(
            status_code=409,
            detail=f"{sup.runtime.mode.value} is already running; stop it first",
        )
    if selected is Mode.V2:
        report = preflight(sup.runtime)
        if report["result"] == "FAIL":
            failed = [c["check"] for c in report["checks"] if c["result"] == "FAIL"]
            raise HTTPException(
                status_code=409,
                detail=f"V2 preflight failed: {', '.join(failed)}",
            )
    try:
        run = await sup.start(selected)
    except (ArcError, ArcFatalError) as exc:
        # A refused start is not a dead process. The dashboard stays up and says
        # why, because the operator's next act is to fix the thing named.
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {
        "status": run.status,
        "mode": run.mode.value,
        # Stated in the response rather than left to be inferred: the operator
        # pressed START and must not believe orders are now going out.
        "execution_armed": run.state.execution_armed,
        "trading_enabled": run.state.trading_enabled,
    }


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
    """STOP RUNTIME. The entire selected runtime shuts down cleanly.

    No websocket, feed, background worker, polling task, execution task, recorder
    or runtime service continues afterwards, and the venue client is closed. The
    process returns to a clean idle state with the dashboard still serving — the
    dashboard outlives the runtime by design, or an operator could never see that
    the stop succeeded.

    Resting orders are NOT cancelled here. Retracting live positions is the
    sweeper's job at market close, and a stop that also flattened the book would
    turn "let me look at this" into a position-closing act.
    """
    sup = _supervisor(request)
    await sup.stop()
    return {"status": sup.status, "mode": sup.mode.value}


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
    _PER_WINDOW_RE = re.compile(r"^majority_w_\d+_\w+$")
    unknown = {k for k in body if k not in _EDITABLE and not _PER_WINDOW_RE.match(k)}
    if unknown:
        raise HTTPException(
            status_code=400,
            detail=f"not editable from the dashboard: {sorted(unknown)}",
        )

    # The full stored row. Writing only the TWAP half would discard MAJORITY's, so
    # this is always the complete dict both engines need — TWAP's half plus whatever
    # MAJORITY keys survived the last save.
    stored = run.store.load_settings() or run.settings.as_storage_dict()
    merged = dict(stored)
    merged.update({k: str(v) for k, v in body.items()})
    try:
        # TWAP half: validated through the same builder the runtime boots with.
        build_trading_config(merged)
        # MAJORITY half: validated through the same builder so a value the
        # dashboard accepts cannot be a value that refuses to start on next launch.
        # MAJORITY keys that are absent (operator sent only TWAP fields) are
        # handled inside build_majority_config: absent+enabled=false is OFF,
        # absent+enabled=true is a ConfigInvariantError.
        from arc.majority.config import build_majority_config

        trading = run.settings.trading
        build_majority_config(
            merged,
            min_tradable_size=trading.min_tradable_size,
            tick_size=trading.tick_size,
        )
    except (ArcError, ArcFatalError) as exc:
        # ConfigInvariantError is fatal at STARTUP, but a rejected dashboard edit is
        # not fatal to a running process — the old configuration is still in force.
        # Letting it propagate would take down the runtime over a typo in a text box.
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    run.store.save_settings(merged, run.clock.now())
    return {
        "saved": True,
        # Both engines' configs are fanned out at construction; a save always requires
        # a restart so the new MajorityEngine and its per-window Repricers are fresh.
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
    since: str = Query("", description="epoch, or a wall clock read in ?tz="),
    until: str = Query("", description="epoch, or a wall clock read in ?tz="),
    tz: str = Query("utc", description="utc | ist | et — the zone since/until are written in"),
    markets: int = Query(50, ge=1, le=1000),
    fmt: str = Query("json", alias="format", description="json | csv | report"),
    validate: bool = Query(False, description="attach the production validation summary"),
) -> Response | dict[str, Any]:
    """The Unified Ledger. The only history there is.

    There is no Orders page and no Trade History page, so every filter, the CSV
    export and the production validation summary are parameters here. A second
    history endpoint would be a second answer to "did that order fill", and a
    thirteenth route for validation would be exactly the diagnostics endpoint the
    contract forbids — the validation report reads the same rows this route
    already serves.
    """
    run = _runtime(request)
    records = search_records(
        ledger_records(run.store, market_limit=markets),
        q,
        direction=direction,
        state=state,
        result=result,
        since=parse_at(since, tz),
        until=parse_at(until, tz),
    )
    rows = [r.as_json() for r in records]
    if fmt == "csv":
        return _csv(rows)
    if fmt == "report" or validate:
        # The runtime's own figures, passed in rather than read from a global: the
        # validator must also be runnable against a database with no process on it.
        now = run.clock.now()
        report = validate_run(
            run.store,
            offsets=tuple(run.settings.trading.windows_by_priority),
            cadence_seconds=MARKET_DURATION_SECONDS,
            market_limit=markets,
            uptime_seconds=max(now - run.started_at, 0.0) if run.started_at else 0.0,
            restarts=run.restart_count,
            reconnects=run.stats.reconnects,
            disconnects=run.stats.disconnects,
            recoveries=run.stats.recoveries,
            chainlink_enabled=run.settings.env.twap_provider.upper() == "CHAINLINK",
            clock=run.clock,
        )
        if fmt == "report":
            return Response(
                render_report(
                    report,
                    mode=run.mode.value,
                    provider=run.settings.env.twap_provider,
                    generated_at=now,
                    gates=run.gate_summary(),
                    verification=run.verification(),
                ),
                media_type="text/plain; charset=utf-8",
            )
        payload = ledger_payload(run, market_limit=markets)
        payload["records"] = rows
        payload["validation"] = report.as_json()
        return payload
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
async def set_strategy_config(
    request: Request,
    strategy_id: str,
    action: str = Query("", description="arm | disarm"),
) -> dict[str, Any]:
    """START TRADING / STOP TRADING. The Limit Order Engine's own control.

    The strategy's PARAMETERS stay read-only here: it is pinned (A17) and buffers,
    windows and submission count are edited on the Settings page. The one thing
    this route does is the operator gate, because arming is the act of the engine
    this route names — the runtime is already up and inert until someone presses
    this.

    `execution_armed` is in-memory and never persisted, so a restart comes back
    disarmed. A gate that survived a crash would re-arm a system nobody was
    watching.
    """
    run = _runtime(request)
    if strategy_id not in default_registry().ids():
        raise HTTPException(status_code=404, detail=f"no strategy {strategy_id}")
    if action == "arm":
        run.arm()
    elif action == "disarm":
        run.disarm()
    else:
        raise HTTPException(
            status_code=405,
            detail=(
                f"{strategy_id} is pinned and not configurable from the dashboard; "
                "edit buffers and windows on the Settings page. "
                "Use ?action=arm or ?action=disarm to start or stop trading"
            ),
        )
    return {
        "id": strategy_id,
        "execution_armed": run.state.execution_armed,
        "trading_enabled": run.state.trading_enabled,
        "paused": run.paused,
    }


# ── majority ────────────────────────────────────────────────────────────────


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
    sup: RuntimeSupervisor | None = getattr(socket.app.state, "supervisor", None)
    run: ArcRuntime | None = sup.runtime if sup is not None else getattr(
        socket.app.state, "runtime", None
    )
    if run is None:  # pragma: no cover - the app is never mounted without a runtime
        await socket.close(code=1011)
        return
    await ws_module.serve(socket, run, (lambda: sup.runtime) if sup is not None else None)


# ── helpers ──────────────────────────────────────────────────────────────────


def _flatten(row: dict[str, Any]) -> dict[str, Any]:
    """One level of nesting into `parent_child` columns.

    The dual-time fields ship as `{"utc": ..., "ist": ..., "et": ...}` so the
    dashboard gets them as one object. A spreadsheet cannot read that — written
    verbatim it becomes the repr of a dict — so each zone becomes its own column
    rather than the export quietly carrying unusable cells.
    """
    out: dict[str, Any] = {}
    for key, value in row.items():
        if isinstance(value, dict):
            out.update({f"{key}_{inner}": v for inner, v in value.items()})
        else:
            out[key] = value
    return out


def _csv(rows: list[dict[str, Any]]) -> Response:
    """CSV only, and every cell already a string.

    The ledger's Decimals arrive here as strings, so writing them verbatim is what
    keeps a spreadsheet from reading 0.740000 as 0.74 and back as a float.
    """
    flat = [_flatten(row) for row in rows]
    buffer = io.StringIO()
    fields = list(flat[0]) if flat else ["market"]
    writer = csv.DictWriter(buffer, fieldnames=fields, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(flat)
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

    stored = run.store.load_settings() or run.settings.as_storage_dict()
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
