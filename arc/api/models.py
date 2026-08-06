"""API payloads. Every Decimal leaves here as a STRING.

The whole module is serialization. No value is computed, compared, or derived —
the dashboard's job is to render what the engines decided, and any arithmetic that
happened here would be a second implementation of a rule that already exists in
one place. That is why `_s` is the only transformation in the file and why the
panels are assembled by reading attributes rather than by combining them.

Decimals become strings because JSON numbers are IEEE doubles. A locked trigger of
120035.000000 would round-trip through a float and come back subtly different, and
the difference would be invisible on screen while being the exact quantity the
window was frozen against.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any

from arc.domain.enums import (
    DISPLAYED_ORDER_STATES,
    LIVE_ORDER_STATES,
    MarketPhase,
    Mode,
    OrderState,
    WindowState,
)
from arc.domain.models import MarketInstance
from arc.domain.money import dec_str
from arc.domain.timing import (
    MARKET_DURATION_SECONDS,
    SETTLEMENT_WINDOW_SECONDS,
    activation_ts,
    format_countdown,
    next_window_ts,
    slug_for,
)
from arc.execution.wallet import WalletSnapshot
from arc.notify.telegram import CATEGORIES, CATEGORY_LABELS
from arc.runtime.ledger import ledger_records, ledger_totals
from arc.strategy.registry import DEFAULT_STRATEGY_ID
from arc.timefmt import clocks, stamps

if TYPE_CHECKING:  # pragma: no cover - annotations only
    from arc.runtime.engine import ArcRuntime

__all__ = [
    "LOE_STAGES",
    "derived_payload",
    "engine_status",
    "ledger_payload",
    "market_payload",
    "preflight",
    "settings_payload",
    "status_payload",
    "strategy_payload",
    "system_payload",
    "wallet_payload",
]

_GREEN = "GREEN"
_YELLOW = "YELLOW"
_RED = "RED"


def _s(value: Decimal | None) -> str | None:
    return None if value is None else dec_str(value)


# ── engine status ────────────────────────────────────────────────────────────


def _light(state: str) -> str:
    if state in ("Running", "Waiting"):
        return _GREEN if state == "Running" else _YELLOW
    if state in ("Reconnecting", "Warning"):
        return _YELLOW
    return _RED


def _row(name: str, state: str, detail: str = "") -> dict[str, str]:
    return {"engine": name, "state": state, "light": _light(state), "detail": detail}


def engine_status(run: ArcRuntime) -> list[dict[str, str]]:
    """The ten engine rows. Each is Running / Waiting / Reconnecting / Warning / Error.

    Waiting is a distinct state from Running on purpose: an engine with nothing to
    do is healthy, and colouring it red would train the operator to ignore red.
    """
    health = run.health()
    market = run.rotator.current
    feed = run.watchdog.status
    feed_state = {"OK": "Running", "WARN": "Warning", "BLOCKED": "Error"}.get(feed, "Waiting")
    if not run.watchdog.has_ticked:
        feed_state = "Waiting"

    gate = run.state.gate
    loe = "Running" if gate.submitting else ("Waiting" if gate.enabled else "Error")
    report = run.recovery_report
    recovery = "Waiting" if report is None else ("Running" if report.ok else "Warning")

    return [
        _row("Market Engine", "Running" if market is not None else "Waiting",
             market.slug if market is not None else "no market open"),
        _row("Window Engine", "Running" if market is not None else "Waiting"),
        _row("Decision Engine", "Running"),
        _row("Risk Engine", "Running" if gate.enabled else "Error", gate.reason),
        _row("Limit Order Engine", loe, "" if gate.armed else "not armed"),
        _row("Recovery Engine", recovery),
        _row("Provider", feed_state, run.feed.url),
        _row("WebSocket", feed_state, f"{run.feed.connect_attempts} connect attempts"),
        _row("RPC", "Running" if run.venue_client is not None else "Waiting",
             "" if run.venue_client is not None else "V1 uses no RPC"),
        _row("Wallet", "Running" if run.venue_client is not None else "Waiting"),
    ] + ([] if health.healthy else [_row("Runtime", "Warning", health.detail)])


# ── preflight ────────────────────────────────────────────────────────────────


def _check(name: str, ok: bool, detail: str = "", *, warn: bool = False) -> dict[str, str]:
    return {
        "check": name,
        "result": "PASS" if ok else ("WARNING" if warn else "FAIL"),
        "detail": detail,
    }


def preflight(run: ArcRuntime) -> dict[str, Any]:
    """The pre-V2 report. A WARNING never blocks; a FAIL does.

    Every line names what is wrong rather than only that something is. A preflight
    that says FAIL with no detail sends the operator to the logs, which is the exact
    trip this dashboard exists to remove.

    Runs in BOTH positions: continuously on the deck against a live runtime, and
    once against the idle runtime immediately before V2 starts. So a check may only
    FAIL on something that is wrong, never on something that has not happened yet —
    an idle process has no feed and no ticks by definition, and failing it for that
    would make V2 unstartable forever.
    """
    trading = run.settings.trading
    gate = run.state.gate
    drift = run.drift.last
    spec = run.spec.result
    report = run.recovery_report
    # Whether this runtime has actually been started. Nothing that can only be true
    # of a running system is allowed to FAIL while it is false: preflight is what
    # V2 must pass BEFORE it starts, so a check that demands a live feed would
    # deadlock the start it guards.
    live = run.status.startswith("RUNNING")
    checks = [
        _check("Configuration", True, f"{len(trading.windows_by_priority)} windows enabled"),
        _check("SQLite", run.store.integrity_check() == "ok", run.store.integrity_check()),
        _check("Runtime", live, run.status, warn=not live),
        _check("Wallet", run.venue_client is not None, "V1 has no venue account", warn=True),
        _check("Provider", run.feed.connect_attempts > 0, run.feed.url, warn=True),
        _check("RTDS", run.watchdog.has_ticked, run.watchdog.status, warn=True),
        _check("Clock", drift is None or not run.health().clock_drift_critical,
               "no reading yet" if drift is None else f"{drift.offset_ms:.0f} ms",
               warn=drift is None),
        # Stale is a FAIL only once there is a feed to be stale. Before the first
        # tick the watchdog reports blocked because nothing has connected yet, which
        # is the normal state of an idle process rather than a fault.
        _check("Feed", not run.watchdog.blocked, run.watchdog.status,
               warn=not run.watchdog.has_ticked),
        _check("PTB", run.stats.ptb_frozen > 0 or run.stats.markets_processed == 0,
               f"{run.stats.ptb_unavailable} unavailable", warn=True),
        _check("Recovery", report is None or report.safe_to_trade,
               "not yet run" if report is None else ", ".join(report.unresolved_orders) or "clean",
               warn=report is None),
        _check("Risk Engine", gate.enabled, gate.reason),
        _check("Decision Engine", True, DEFAULT_STRATEGY_ID),
        _check("Limit Order Engine", gate.armed, "not armed", warn=True),
        _check("WebSocket", True, "serving"),
        _check("RPC", run.venue_client is not None, "V1 uses no RPC", warn=True),
    ]
    worst = "FAIL" if any(c["result"] == "FAIL" for c in checks) else (
        "WARNING" if any(c["result"] == "WARNING" for c in checks) else "PASS"
    )
    return {"result": worst, "checks": checks, "ready": live and worst != "FAIL",
            "spec_status": spec.status.value,
            "spec_reason": spec.reason, "spec_unresolved": list(spec.unresolved())}


# ── market and windows ───────────────────────────────────────────────────────


def _window_payload(run: ArcRuntime, market: MarketInstance, offset: int) -> dict[str, Any]:
    window = market.windows[offset]
    return {
        "offset_seconds": offset,
        "label": f"{offset}s",
        "state": window.state.value,
        # NO_DIRECTION is displayed verbatim and never replaced by a guess. It is a
        # terminal, legitimate outcome, and inventing UP or DOWN here would trade a
        # direction the freeze explicitly refused to determine.
        "direction": window.direction.value if window.direction is not None
        else (WindowState.NO_DIRECTION.value if window.state is WindowState.NO_DIRECTION else ""),
        "opening_twap": _s(window.opening_twap),
        "ptb": _s(window.ptb),
        "buffer": _s(window.buffer),
        "locked_trigger": _s(window.locked_trigger),
        "frozen_at": window.frozen_at,
        "fired_at": window.fired_at,
        "opens_at": activation_ts(market.close_ts, offset),
        # Every execution window's own moments in both zones. The epochs above stay
        # canonical; these are what the LOE panel prints beside them.
        "opens_at_display": stamps(activation_ts(market.close_ts, offset)),
        "frozen_at_display": stamps(window.frozen_at),
        "fired_at_display": stamps(window.fired_at),
        "configured_buffer": _s(run.settings.trading.buffer_for(offset)),
        "implied_btc_move": _s(run.settings.trading.implied_btc_move(offset)),
    }


def market_payload(run: ArcRuntime, now: float) -> dict[str, Any]:
    """The current market, its windows, and both official timers' inputs.

    The countdown is sent as the server's own MM:SS alongside close_ts. The browser
    animates between ticks from close_ts, but the definitive string comes from
    here — two clocks producing two timers is exactly the drift the two-timer
    requirement forbids.
    """
    market = run.rotator.current
    if market is None:
        window_ts = next_window_ts(int(now) // MARKET_DURATION_SECONDS * MARKET_DURATION_SECONDS)
        return {
            "slug": "", "phase": "", "window_ts": None, "close_ts": None,
            "countdown": "00:00", "next_market": slug_for(window_ts),
            "ptb": None, "signal_twap": None, "settlement_twap": None,
            "settlement_window_seconds": SETTLEMENT_WINDOW_SECONDS,
            "observation_count": 0, "windows": [], "closing": None,
            "opens_display": stamps(None), "closes_display": stamps(None),
        }
    closing = run.rotator.closing
    return {
        "slug": market.slug,
        "phase": market.phase.value,
        "window_ts": market.window_ts,
        "close_ts": market.close_ts,
        "countdown": format_countdown(now, market.close_ts),
        # The market's own five minutes, in both zones. The countdown above is the
        # canonical timer; these are the wall clock either side of it, so an
        # operator can line the market up against the Polymarket page without
        # doing timezone arithmetic during the last ten seconds of a window.
        "opens_display": stamps(market.window_ts),
        "closes_display": stamps(market.close_ts),
        "next_market": slug_for(next_window_ts(market.window_ts)),
        "ptb": _s(market.ptb),
        "signal_twap": _s(market.signal_twap),
        "settlement_twap": _s(run.settlement_twap(market.slug)),
        "settlement_window_seconds": SETTLEMENT_WINDOW_SECONDS,
        "observation_count": market.observation_count,
        "windows": [_window_payload(run, market, o) for o in sorted(market.windows)],
        "closing": None if closing is None else {
            "slug": closing.slug, "phase": closing.phase.value,
            "settlement_twap": _s(run.settlement_twap(closing.slug)),
        },
    }


# ── execution ────────────────────────────────────────────────────────────────


def execution_payload(run: ArcRuntime) -> dict[str, Any]:
    """The Execution Panel. Counts come from persisted order rows, not memory.

    Counted from storage so the panel and the ledger cannot disagree: an in-memory
    tally that missed one state transition would show a filled order as working
    forever, and the operator would chase an order that had already settled.
    """
    counts = dict.fromkeys(DISPLAYED_ORDER_STATES, 0)
    exposure = Decimal("0")
    working = 0
    for record in ledger_records(run.store, market_limit=20):
        display = record.state_display
        if display in counts:
            counts[display] += 1
        if record.state in {s.value for s in LIVE_ORDER_STATES}:
            working += 1
    for order in run.store.live_orders():
        exposure += order.price * order.remaining_size
    return {
        "mode": run.mode.value,
        "execution_label": "Paper" if run.mode is Mode.V1 else "Live",
        "submission_count": run.settings.trading.submission_count,
        "position_notional_usd": _s(run.settings.trading.position_notional_usd),
        "current_exposure": _s(exposure),
        "open_orders": len(run.store.live_orders()),
        "working_orders": working,
        "orders_by_state": counts,
        "orders_submitted": run.stats.orders_submitted,
        "orders_repriced": run.stats.orders_repriced,
        "fills_recorded": run.stats.fills_recorded,
    }


# ── wallet ───────────────────────────────────────────────────────────────────

_UNAVAILABLE = "UNAVAILABLE (Official API not available)"


def wallet_payload(snapshot: WalletSnapshot) -> dict[str, Any]:
    """Q3: a field with no official source is the literal UNAVAILABLE string.

    Sent as that string rather than as null so the browser cannot render it as a
    blank that looks like zero. An operator sizing a position against a blank reads
    it as "nothing at risk".
    """
    def money(value: Decimal | None) -> str:
        return _UNAVAILABLE if value is None else dec_str(value)

    ledger = snapshot.ledger
    return {
        "address": snapshot.address,
        "status": snapshot.status,
        "network": snapshot.network,
        "provider": snapshot.provider,
        "credentialed": snapshot.credentialed,
        "available_balance": money(snapshot.available_balance),
        "reserved_balance": money(snapshot.reserved_balance),
        "balance_in_open_positions": money(snapshot.balance_in_open_positions),
        "buying_power": money(snapshot.buying_power),
        "total_account_value": money(snapshot.total_account_value),
        "current_exposure": money(snapshot.current_exposure),
        "current_position_value": money(snapshot.current_position_value),
        "pending_position_value": dec_str(snapshot.pending_position_value),
        "open_position_count": snapshot.open_position_count,
        "unrealized_pnl": money(snapshot.unrealized_pnl),
        "realized_today": dec_str(ledger.realized_today),
        "realized_run": dec_str(ledger.realized_run),
        "realized_lifetime": dec_str(ledger.realized_lifetime),
        "largest_win": dec_str(ledger.largest_win),
        "largest_loss": dec_str(ledger.largest_loss),
        "winning_streak": ledger.winning_streak,
        "losing_streak": ledger.losing_streak,
        "markets_settled": ledger.markets_settled,
        "wins": ledger.wins,
        "losses": ledger.losses,
    }


# ── settings, strategies, system ─────────────────────────────────────────────


def settings_payload(run: ArcRuntime) -> dict[str, Any]:
    """Editable fields, plus the lock that applies while trading is armed."""
    trading = run.settings.trading
    return {
        # Locked while armed. Editing a buffer mid-execution would change the
        # configuration a resting order was approved under, and the order would
        # then be enforcing a rule that no longer exists.
        "locked": run.state.execution_armed,
        "buffers": {str(o): dec_str(trading.buffer_for(o)) for o in trading.windows_by_priority},
        "execution_windows": list(trading.windows_by_priority),
        "submission_count": trading.submission_count,
        "position_notional_usd": dec_str(trading.position_notional_usd),
        "max_trades_per_market": trading.max_trades_per_market,
        "max_concurrent_positions": trading.max_concurrent_positions,
        "max_daily_loss_usd": dec_str(trading.max_daily_loss_usd),
        "max_consecutive_losses": trading.max_consecutive_losses,
        "entry_price_min": dec_str(trading.entry_price_min),
        "entry_price_max": dec_str(trading.entry_price_max),
        "tick_size": dec_str(trading.tick_size),
        "min_tradable_size": dec_str(trading.min_tradable_size),
        "allow_opposing_directions": trading.allow_opposing_directions,
        "implied_btc_move": {
            str(o): dec_str(trading.implied_btc_move(o)) for o in trading.windows_by_priority
        },
        # Read-only text, never a selector. There is exactly one strategy (A17) and
        # a dropdown with one entry invites the assumption that others exist.
        "strategy": DEFAULT_STRATEGY_ID,
        "strategy_editable": False,
        "provider": run.settings.env.twap_provider,
        # Presentation, shipped rather than hardcoded in the markup so a theme or a
        # repaint cadence set in .env is the one the browser actually uses.
        "theme": run.settings.env.theme,
        "refresh_rate_ms": run.settings.env.refresh_rate_ms,
        # Twenty-one independent toggles, carried on /settings rather than a route of
        # their own: the twelve-route surface is an acceptance criterion (A15).
        "notifications": {
            name: run.notifier.wants(name) for name in CATEGORIES
        },
        "notification_labels": dict(CATEGORY_LABELS),
        "telegram_configured": run.notifier.configured,
        "warnings": list(run.settings.warnings),
    }


def strategy_payload(run: ArcRuntime) -> list[dict[str, Any]]:
    from arc.strategy.registry import default_registry

    registry = default_registry()
    return [
        {
            "id": d.strategy_id,
            "name": d.name,
            "description": d.description,
            "pinned": d.pinned,
            "disableable": d.disableable,
            "active": d.strategy_id == DEFAULT_STRATEGY_ID,
        }
        for d in registry.describe_all()
    ]


def _memory() -> str:
    """Total RAM, read from /proc/meminfo. UNAVAILABLE off Linux.

    stdlib has no portable memory reading and psutil is not a dependency. A guess
    would be worse than the literal string: an operator sizing a VPS against a
    fabricated total is the exact failure Q3 forbids for the wallet, and the same
    rule applies to every read-only field.
    """
    try:
        for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
            if line.startswith("MemTotal:"):
                return f"{int(line.split()[1]) / 1e6:.1f} GB"
    except OSError:
        pass
    return _UNAVAILABLE


def _git_commit() -> str:
    """HEAD, read from .git. No subprocess: `arc run` must not shell out."""
    git = Path(__file__).resolve().parent.parent.parent / ".git"
    try:
        head = (git / "HEAD").read_text(encoding="utf-8").strip()
        if head.startswith("ref: "):
            return (git / head[5:]).read_text(encoding="utf-8").strip()[:12]
        return head[:12]
    except OSError:
        return _UNAVAILABLE


def system_payload(run: ArcRuntime, now: float) -> dict[str, Any]:
    """Read-only host and process facts. Nothing here is estimated."""
    import os
    import platform
    import shutil
    import sys

    from arc import __version__

    usage = shutil.disk_usage(str(run.store.path.parent))
    return {
        "hostname": platform.node(),
        "operating_system": f"{platform.system()} {platform.release()}",
        "cpu": platform.processor() or platform.machine(),
        "cpu_count": os.cpu_count(),
        "memory": _memory(),
        "disk_total_gb": round(usage.total / 1e9, 1),
        "disk_free_gb": round(usage.free / 1e9, 1),
        # PM2 sets these in the child's environment. Absent means "not under PM2",
        # which is a fact, not a failure — `arc run` in a terminal is supported.
        # Lowercase deliberately: PM2 exports `pm2_id`, not `PM2_ID`. Capitalising it
        # to satisfy the linter would read an env var PM2 never sets.
        "pm2_status": f"managed (id {os.environ['pm2_id']})"  # noqa: SIM112
        if "pm2_id" in os.environ
        else "not managed by PM2",
        "git_commit": _git_commit(),
        "python_version": sys.version.split()[0],
        "arc_version": __version__,
        "sqlite_status": run.store.integrity_check(),
        "sqlite_path": str(run.store.path),
        "sqlite_tables": len(run.store.table_names()),
        "schema_version": run.store.schema_version(),
        "active_provider": run.settings.env.twap_provider,
        "wallet": run.venue_client is not None,
        "rtds": run.watchdog.status,
        "websocket": run.feed.url,
        "rpc": run.venue_client is not None,
        # The endpoints and chain this process is actually dialling, read from the
        # live configuration rather than from constants in the markup. An operator
        # who overrode a CLOB host in .env must be able to confirm the override took
        # without SSHing in to read the file back.
        "clob_host": run.settings.env.clob_host,
        "clob_http_url": run.settings.env.clob_http_url,
        "clob_ws_url": run.settings.env.clob_ws_url,
        "network_id": run.settings.env.network_id,
        "chain_id": run.settings.env.chain_id,
        "pm2_name": run.settings.env.pm2_name,
        "log_level": run.settings.env.log_level,
        "timezone": run.settings.env.timezone,
        "restart_count": run.restart_count,
        "runtime_uptime_seconds": max(now - run.started_at, 0.0) if run.started_at else 0.0,
        "mode": run.mode.value,
    }


# ── ledger and analytics ─────────────────────────────────────────────────────


def ledger_payload(run: ArcRuntime, *, market_limit: int = 50) -> dict[str, Any]:
    records = ledger_records(run.store, market_limit=market_limit)
    return {"records": [r.as_json() for r in records], "totals": ledger_totals(records)}


# ── the Limit Order Engine's one visible stage ───────────────────────────────

# The lifecycle the LOE panel highlights. Ordered, and resolved HERE rather than in
# the browser: the stage is a statement about engine state, and a frontend that
# inferred it from a handful of fields would be a second implementation of the
# lifecycle that could disagree with the one that actually runs.
LOE_STAGES: tuple[str, ...] = (
    "WAITING_FOR_WINDOW",
    "WINDOW_OPEN",
    "VALUES_FROZEN",
    "INTENT_CREATED",
    "ORDER_SUBMITTED",
    "WAITING_FOR_FILL",
    "FILLED",
    "BUFFER_NOT_SATISFIED",
    "SETTLEMENT",
)


def _active_window(market: MarketInstance | None) -> Any:
    """The window the operator is watching: the frozen one still awaiting its fire.

    Falls back to the most recently frozen window so the panel keeps showing the
    values that were locked rather than blanking the instant a window fires — the
    frozen numbers are what the operator checks the fill against.
    """
    if market is None:
        return None
    frozen = [w for w in market.windows.values() if w.state is WindowState.FROZEN]
    if frozen:
        return min(frozen, key=lambda w: w.offset_seconds)
    fired = [w for w in market.windows.values() if w.frozen_at is not None]
    return max(fired, key=lambda w: w.frozen_at or 0.0) if fired else None


def derived_payload(run: ArcRuntime, market: MarketInstance | None) -> dict[str, Any]:
    """The active window's frozen values and the LOE stage. Read, never computed.

    Every field is copied off an engine object or a persisted order row. Nothing
    here compares a TWAP to a trigger or decides a direction; the window already
    did both at its freeze.
    """
    window = _active_window(market)
    states = (
        set()
        if market is None
        else {o.state for o in run.store.orders_for(market.slug)}
    )

    # Order state wins over window state, and the order is the priority order of the
    # panel: an order that has filled is the furthest thing that happened, and
    # showing the window's stage instead would put the operator a step behind the
    # money. Submission and fill are separate stages here for the same reason.
    if market is not None and market.phase in (MarketPhase.SETTLING, MarketPhase.SETTLED):
        stage = "SETTLEMENT"
    elif OrderState.FILLED in states:
        stage = "FILLED"
    elif states & {OrderState.SUBMITTED, OrderState.PARTIAL, OrderState.INDETERMINATE}:
        stage = "WAITING_FOR_FILL"
    elif OrderState.PENDING in states:
        stage = "ORDER_SUBMITTED"
    elif window is None:
        stage = "WAITING_FOR_WINDOW"
    elif window.state is WindowState.FIRED:
        stage = "INTENT_CREATED"
    elif window.state in (WindowState.EXPIRED, WindowState.NO_DIRECTION):
        stage = "BUFFER_NOT_SATISFIED"
    elif window.state is WindowState.FROZEN:
        stage = "VALUES_FROZEN"
    else:
        stage = "WINDOW_OPEN"

    return {
        "loe_stage": stage,
        "loe_stages": list(LOE_STAGES),
        "current_window": "" if window is None else f"{window.offset_seconds}s",
        # NO_DIRECTION is shown verbatim, never replaced with a guess.
        "frozen_direction": (
            ""
            if window is None
            else (
                window.direction.value
                if window.direction is not None
                else (
                    WindowState.NO_DIRECTION.value
                    if window.state is WindowState.NO_DIRECTION
                    else ""
                )
            )
        ),
        "locked_trigger": None if window is None else _s(window.locked_trigger),
        "buffer": None if window is None else _s(window.buffer),
        "frozen_twap": None if window is None else _s(window.opening_twap),
    }


# ── the one status document ──────────────────────────────────────────────────


async def status_payload(run: ArcRuntime, now: float) -> dict[str, Any]:
    """Everything the dashboard renders, from ONE read of runtime state.

    Assembled in one place so OPS Deck, Signal Tank, Ledger, Analytics and System
    can never disagree. Panels that each fetched their own slice would each show a
    different instant, and a market boundary landing between two fetches would
    display one market's PTB against another's TWAP.
    """
    gate = run.state.gate
    health = run.health()
    report = run.recovery_report
    return {
        "ts": now,
        # The OPS Deck's wall clocks. Rendered on the backend from the same `now`
        # every other field on this document was built from, so the clocks and the
        # values beside them describe one instant. A browser converting its own
        # Date() would drift from the runtime it is reporting on.
        "clocks": clocks(now),
        "runtime": {
            "status": run.status,
            "mode": run.mode.value,
            "started_at": run.started_at,
            "uptime_seconds": max(now - run.started_at, 0.0) if run.started_at else 0.0,
            # Both gates, independently, always. One combined "can trade" boolean
            # would hide state 4 — system disabled while the operator is armed — and
            # that is the state the operator most needs named.
            "trading_enabled": gate.enabled,
            "execution_armed": gate.armed,
            # The third flag. Pause holds new submissions without disarming, so an
            # operator who paused and forgot would otherwise see an armed runtime
            # that silently never submits.
            "paused": run.paused,
            "disable_reason": gate.reason,
            "spec_status": run.state.spec_status.value,
            "feed_age_ms": health.feed_age_ms,
            "clock_drift_ms": health.clock_drift_ms,
        },
        "engines": engine_status(run),
        "market": market_payload(run, now),
        # Resolved on the backend, like everything else. The LOE stage and the active
        # window's frozen values are statements about engine state, and a browser that
        # inferred them would be a second lifecycle implementation.
        "derived": derived_payload(run, run.rotator.current),
        "execution": execution_payload(run),
        # Through the runtime, not the reader directly: the runtime is what notices a
        # CONNECTED -> DISCONNECTED transition and logs it once. Reading the reader
        # here would render the change and report it to nobody.
        "wallet": wallet_payload(await run.wallet_snapshot(now)),
        "recovery": {
            "running": report is None,
            "stage": "COMPLETE" if report is not None else "PENDING",
            "steps": [] if report is None else [
                {"step": s.step.value, "ok": s.ok, "detail": s.detail} for s in report.steps
            ],
            "markets_recovered": 0 if report is None else len(report.resumed_markets),
            "unresolved_orders": [] if report is None else list(report.unresolved_orders),
            "orphans": [] if report is None else list(report.orphans),
            "safe_to_trade": report is not None and report.safe_to_trade,
        },
        "stats": {
            "markets_processed": run.stats.markets_processed,
            "ptb_frozen": run.stats.ptb_frozen,
            "ptb_unavailable": run.stats.ptb_unavailable,
            "observations_accepted": run.stats.observations_accepted,
            "observations_rejected": run.stats.observations_rejected,
            "settlement_samples": run.stats.settlement_samples,
            "settlement_stream_found": run.stats.settlement_stream_found,
            "reconnects": run.stats.reconnects,
            "disconnects": run.stats.disconnects,
            "recoveries": run.stats.recoveries,
            "orders_submitted": run.stats.orders_submitted,
            "orders_repriced": run.stats.orders_repriced,
            "fills_recorded": run.stats.fills_recorded,
        },
        "preflight": preflight(run),
        "settings": settings_payload(run),
        "system": system_payload(run, now),
    }
