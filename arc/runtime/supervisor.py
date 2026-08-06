"""The runtime supervisor. Starts, stops and switches V1 and V2.

WHY THIS EXISTS. `arc run` used to own the dashboard as a child task, which made
the dashboard strictly shorter-lived than the runtime it displayed — so the
dashboard could not stop the runtime without killing itself. The Runtime Mode
Contract requires the opposite: the operator selects V1 or V2, presses START, and
the whole paper or live system comes up; presses STOP and it all goes away, with
the dashboard still there to show that it did. So the ownership is inverted here.
The supervisor is what the dashboard holds; the runtime is what the supervisor
holds, and it is thrown away and rebuilt on every start.

REBUILT, NEVER REUSED. `start()` constructs a brand-new ArcRuntime, a new feed, a
new executor, a new venue client and a new HTTP client every time. Nothing is
carried across a stop. That is the whole of the isolation requirement: V1 and V2
cannot share execution state, active orders, websocket tasks, providers, wallet
sessions or adapters, because after a stop none of those objects exist any more.
Reusing a stopped runtime would be cheaper and would be exactly the bug — a live
adapter left holding a paper run's order ids is a real order cancelled against a
simulated one.

THE INERT RUNTIME. Between stops there is still an ArcRuntime object, built for
the selected mode but never started. It exists so the dashboard has something to
render: every panel reads from a runtime, and a supervisor that held None between
runs would need a second, parallel "idle" document that could disagree with the
real one. The inert runtime opens no sockets — the feed connects lazily inside
`messages()`, and V2's authenticated client is built at start, never at rest, so
an idle process holds no venue session.

STARTING IS NOT TRADING. `start()` brings up feeds, TWAP, discovery, PTB, CLOB,
recovery, recorder, Signal Tank, Telegram, dashboard state and every engine, with
`execution_armed` FALSE. The operator arms trading separately, afterwards, from
the Limit Order Engine. A start that also armed would mean the act of looking at
the system is the act of trading with it.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any, Final, TextIO

import polymarket

from arc.clock import Clock
from arc.config import Settings
from arc.domain.enums import Mode
from arc.errors import ArcError
from arc.logging_setup import log_event
from arc.market.discovery import MarketDiscovery, build_discovery
from arc.market.feed import BackoffPolicy
from arc.market.providers import build_provider
from arc.runtime.engine import (
    EXPECTED_SYMBOL,
    ArcRuntime,
    RuntimeStatus,
    TokenCache,
    build_executor,
)
from arc.runtime.state import RuntimeState
from arc.storage.store import Store

__all__ = ["RuntimeSupervisor"]

# How long a stop waits for the run task to unwind before it stops waiting. The
# runtime's own `finally` cancels the feed and notifier tasks and awaits them, so
# this is a backstop against a task that refuses to die, not the normal path. A
# stop that hung forever would leave the dashboard showing STOPPING with no way
# out except killing the process.
_SHUTDOWN_TIMEOUT: Final[float] = 15.0


class RuntimeSupervisor:
    """Owns at most one ArcRuntime at a time. The dashboard owns this.

    Not a registry and not a pool: `runtime` is one object, and starting a mode
    replaces it. There is deliberately no way to hold two, because two live
    runtimes in one process is the failure the isolation rule names.
    """

    __slots__ = (
        "_client",
        "_clock",
        "_discovery",
        "_lock",
        "_logger",
        "_out",
        "_settings",
        "_store",
        "_task",
        "mode",
        "runtime",
    )

    def __init__(
        self,
        *,
        settings: Settings,
        store: Store,
        clock: Clock,
        out: TextIO,
        logger: logging.Logger | None = None,
    ) -> None:
        self._settings = settings
        self._store = store
        self._clock = clock
        self._out = out
        self._logger = logger
        self._task: asyncio.Task[Any] | None = None
        self._discovery: MarketDiscovery | None = None
        self._client: polymarket.AsyncSecureClient | None = None
        # Serialises start/stop/switch. Two operators on two browser tabs pressing
        # START and STOP within the same tick would otherwise interleave a teardown
        # with a construction and leave a half-built runtime attached.
        self._lock = asyncio.Lock()
        self.mode = settings.mode
        # Built inert so the dashboard has something to render before the first
        # start. See the module docstring.
        self.runtime = self._build_inert(self.mode)

    # ── read ─────────────────────────────────────────────────────────────────

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    @property
    def status(self) -> str:
        """The runtime's own status. Never computed separately from it.

        Read through the runtime rather than tracked here as a second field: two
        places recording whether the system is running is two answers to the
        question the operator is actually asking.
        """
        return self.runtime.status

    # ── lifecycle ────────────────────────────────────────────────────────────

    async def start(self, mode: Mode, *, market_target: int | None = None) -> ArcRuntime:
        """START RUNTIME. Brings up the entire selected system; arms nothing.

        Raises ArcError if a runtime is already up. Not a silent restart: an
        operator who pressed START twice must be told the second press did
        nothing, rather than have the first run's markets and accumulators
        discarded underneath them.

        `market_target` stops after N markets. Test and rehearsal only — the
        dashboard never sends it, because a production run that quietly ended
        after a fixed number of markets is a bot that stopped trading without
        anyone pressing STOP.
        """
        async with self._lock:
            if self.running:
                raise ArcError(
                    f"{self.runtime.mode.value} is already running; stop it before "
                    "starting another runtime"
                )
            # A fresh object graph every time. See the module docstring.
            await self._teardown_resources()
            self.mode = mode
            self.runtime = await self._build_live(mode)
            self._task = asyncio.create_task(
                self.runtime.run(market_target=market_target), name=f"arc-runtime-{mode.value}"
            )
            # Hand the loop one turn so `run()` reaches STARTING and binds the event
            # hub before the caller renders a status frame. Without it the first
            # frame after START reports STOPPED, which reads as a failed start.
            await asyncio.sleep(0)
            return self.runtime

    async def wait(self) -> None:
        """Block until the running runtime finishes on its own. No-op when idle.

        Only a bounded run (`market_target`) ever finishes by itself; the
        production loop runs until stopped. Errors propagate rather than being
        swallowed here, because a runtime that died of a fatal is not the same
        outcome as one an operator stopped.
        """
        task = self._task
        if task is not None:
            with contextlib.suppress(asyncio.CancelledError):
                # Shielded so that an operator pressing STOP ends the wait rather
                # than cancelling the caller. Without it a dashboard-initiated stop
                # would propagate CancelledError up into `arc run` and take the
                # dashboard down with the runtime it just stopped.
                await asyncio.shield(task)

    async def stop(self) -> None:
        """STOP RUNTIME. Nothing may still be running when this returns.

        Feeds, websockets, background workers, polling tasks, execution tasks,
        the recorder and every runtime service go down together, and the venue
        session and HTTP client are closed. The process returns to idle with a
        fresh inert runtime for the same mode, so the dashboard keeps rendering.
        """
        async with self._lock:
            await self._stop_locked()

    async def switch(self, mode: Mode) -> ArcRuntime:
        """Change runtime. Always a full stop first, even for the same mode.

        Same mode included on purpose: "restart V1" and "switch to V2" must be
        the same code path, because a switch that skipped the teardown when the
        mode happened to match is a switch that keeps the old feed alive.
        """
        async with self._lock:
            await self._stop_locked()
        return await self.start(mode)

    async def aclose(self) -> None:
        """Process shutdown. Stops the runtime if one is up."""
        await self.stop()

    # ── construction ─────────────────────────────────────────────────────────

    def _build_inert(self, mode: Mode) -> ArcRuntime:
        """A runtime that exists to be read, not run.

        The paper executor is used regardless of mode. Nothing is executed here —
        the object is replaced wholesale by `_build_live` before anything runs —
        and building a LIVE executor would mean an idle ARC holds an authenticated
        venue session with a signing key loaded, for a run that has not started.
        """
        from arc.execution.v1_paper import PaperExecutor

        runtime = RuntimeState(self._store, self._clock)
        runtime.load()
        # Tracked like the live one so the next teardown closes it. An untracked
        # client here would leak one connection pool per stop, and a process an
        # operator start/stopped all day would accumulate them silently.
        self._discovery = build_discovery(logger=self._logger)
        return ArcRuntime(
            settings=self._settings,
            store=self._store,
            clock=self._clock,
            runtime=runtime,
            discovery=self._discovery,
            feed=self._build_feed(),
            executor=PaperExecutor(),
            out=self._out,
            logger=self._logger,
        )

    async def _build_live(self, mode: Mode) -> ArcRuntime:
        """The real thing, for one mode, with its own connections."""
        settings = self._settings if self._settings.mode is mode else self._settings.with_mode(mode)
        self._discovery = build_discovery(logger=self._logger)

        runtime = RuntimeState(self._store, self._clock)
        runtime.load()
        tokens = TokenCache()
        executor, client = await build_executor(
            settings, self._store, tokens, logger=self._logger
        )
        self._client = client
        run = ArcRuntime(
            settings=settings,
            store=self._store,
            clock=self._clock,
            runtime=runtime,
            discovery=self._discovery,
            feed=self._build_feed(),
            executor=executor,
            out=self._out,
            venue_client=client,
            logger=self._logger,
        )
        # The runtime's own cache, so the resolver the executor holds is the one the
        # market loop fills. Two caches would let the executor resolve from an empty
        # one and refuse every submission on a perfectly healthy market.
        run.tokens = tokens
        return run

    def _build_feed(self) -> Any:
        env = self._settings.env
        return build_provider(
            env.twap_provider,
            self._clock,
            url=env.rtds_url,
            backoff=BackoffPolicy(
                initial_seconds=env.reconnect_backoff_ms / 1000,
                max_seconds=env.reconnect_backoff_max_ms / 1000,
            ),
            chainlink_api_key=env.chainlink_api_key.get_secret_value(),
            chainlink_api_secret=env.chainlink_api_secret.get_secret_value(),
            chainlink_feed_id=env.chainlink_feed_id,
            chainlink_decimals=env.chainlink_decimals,
            chainlink_ws_url=env.chainlink_ws_url,
            symbol=EXPECTED_SYMBOL,
            logger=self._logger,
        )

    # ── teardown ─────────────────────────────────────────────────────────────

    async def _stop_locked(self) -> None:
        """The teardown itself. Caller holds the lock."""
        task, self._task = self._task, None
        if task is not None and not task.done():
            self.runtime.status = RuntimeStatus.STOPPING
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, asyncio.TimeoutError, ArcError):
                await asyncio.wait_for(task, timeout=_SHUTDOWN_TIMEOUT)
            if not task.done():
                # Reported, not hidden. A task that outlived its cancellation is
                # still holding a socket, and an operator told "stopped" would then
                # start the other mode alongside it.
                log_event(
                    logging.ERROR,
                    "Runtime Shutdown Incomplete",
                    f"{self.runtime.mode.value} did not stop within "
                    f"{_SHUTDOWN_TIMEOUT:.0f}s",
                    logger=self._logger,
                )
        await self._teardown_resources()
        # A fresh inert runtime, not the stopped one: the stopped object still holds
        # the run's markets, accumulators and validator history, and showing them
        # under a STOPPED banner invites reading them as current.
        self.runtime = self._build_inert(self.mode)

    async def _teardown_resources(self) -> None:
        """Close the venue client and the discovery's HTTP client. Idempotent."""
        client, self._client = self._client, None
        discovery, self._discovery = self._discovery, None
        if client is not None:
            with contextlib.suppress(Exception):
                await client.close()
        if discovery is not None:
            with contextlib.suppress(Exception):
                await discovery.aclose()
