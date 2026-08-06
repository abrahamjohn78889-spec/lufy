"""The runtime lifecycle: start, stop, switch, and the isolation between V1 and V2.

The contract these tests hold to is that STARTING IS NOT TRADING and that nothing
survives a stop. Both failures are invisible while they are happening:

  armed by start     the operator pressed START to look at the system and the
                     next window submits a real order.
  shared state       a stopped runtime's executor, feed or venue session reused
                     by the next one is a live adapter holding a paper run's
                     order ids — a real order cancelled against a simulated one.
  leaked task        a run task that outlived its cancellation still holds a
                     socket, and an operator told "STOPPED" then starts the other
                     mode alongside it.

ArcRuntime.run is replaced throughout. This file is about who owns what and what
is torn down, not about the market loop — running the real loop would need a live
feed and a venue, and the engine's own behaviour is tested elsewhere.
"""

from __future__ import annotations

import asyncio
import io
import logging
from collections.abc import Iterator
from typing import Any

import pytest
from conftest import VALID_TRADING_VALUES
from fastapi.testclient import TestClient

from arc.api.app import build_app
from arc.api.routes import ROUTE_PATHS, router
from arc.clock import FrozenClock
from arc.config import ArcSettings, Settings, build_trading_config
from arc.domain.enums import Mode
from arc.errors import ArcError, ConfigInvariantError
from arc.execution.v1_paper import PaperExecutor
from arc.runtime.engine import ArcRuntime, RuntimeStatus
from arc.runtime.supervisor import RuntimeSupervisor
from arc.storage.store import Store

_NOW = 1_754_400_000.0

_VENUE_KEYS = {
    "polymarket_api_key": "k",
    "polymarket_api_secret": "s",
    "polymarket_api_passphrase": "p",
    "polymarket_private_key": "0xk",
}


def _settings(**env: Any) -> Settings:
    return Settings(
        env=ArcSettings(_env_file=None, **env),
        trading=build_trading_config(dict(VALID_TRADING_VALUES)),
        seeded_from_env=False,
    )


@pytest.fixture(autouse=True)
def _no_market_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    """The runtime runs, reports RUNNING and waits to be cancelled.

    Enough to make the lifecycle real — there is a task, it can be cancelled, and
    the status the dashboard reads is the runtime's own — without a feed or a venue.
    """

    async def _run(self: ArcRuntime, *, market_target: int | None = None) -> None:
        self.status = RuntimeStatus.running_for(self.mode)
        await asyncio.Event().wait()

    monkeypatch.setattr(ArcRuntime, "run", _run)


def _supervisor(tmp_path: Any, settings: Settings | None = None) -> RuntimeSupervisor:
    store = Store(f"{tmp_path}/arc.db")
    store.migrate(_NOW)
    return RuntimeSupervisor(
        settings=settings or _settings(),
        store=store,
        clock=FrozenClock(_NOW),
        out=io.StringIO(),
        logger=logging.getLogger("arc.test.lifecycle"),
    )


class TestTheIdleProcess:
    def test_there_is_a_runtime_to_render_before_the_first_start(self, tmp_path: Any) -> None:
        """The dashboard reads a runtime for every panel. A supervisor holding None
        between runs would need a second, parallel "idle" document to render from,
        and two documents can disagree."""
        sup = _supervisor(tmp_path)
        assert sup.running is False
        assert sup.status == RuntimeStatus.STOPPED

    def test_the_idle_runtime_holds_no_venue_session(self, tmp_path: Any) -> None:
        """Even when V2 is selected. An idle ARC must not hold an authenticated
        session with a signing key loaded for a run nobody started."""
        sup = _supervisor(tmp_path, _settings(mode="V2", **_VENUE_KEYS))
        assert isinstance(sup.runtime.executor, PaperExecutor)
        assert sup.runtime.venue_client is None


class TestStartIsNotTrading:
    def test_starting_v1_arms_nothing(self, tmp_path: Any) -> None:
        sup = _supervisor(tmp_path)

        async def go() -> tuple[str, bool]:
            run = await sup.start(Mode.V1)
            await asyncio.sleep(0)
            result = (run.status, run.state.execution_armed)
            await sup.stop()
            return result

        status, armed = asyncio.run(go())
        assert status == RuntimeStatus.RUNNING_V1
        assert armed is False

    def test_a_second_start_is_refused_rather_than_a_silent_restart(
        self, tmp_path: Any
    ) -> None:
        """A restart would discard the first run's markets and accumulators under
        an operator who thinks they pressed a no-op."""
        sup = _supervisor(tmp_path)

        async def go() -> None:
            await sup.start(Mode.V1)
            try:
                with pytest.raises(ArcError, match="already running"):
                    await sup.start(Mode.V1)
            finally:
                await sup.stop()

        asyncio.run(go())


class TestStopLeavesNothingRunning:
    def test_the_run_task_is_gone_and_the_status_is_stopped(self, tmp_path: Any) -> None:
        sup = _supervisor(tmp_path)

        async def go() -> tuple[bool, str, int]:
            await sup.start(Mode.V1)
            await sup.stop()
            leftovers = [
                t for t in asyncio.all_tasks() if t.get_name().startswith("arc-runtime-")
            ]
            return sup.running, sup.status, len(leftovers)

        running, status, leftovers = asyncio.run(go())
        assert running is False
        assert status == RuntimeStatus.STOPPED
        assert leftovers == 0

    def test_the_discovery_client_is_closed(self, tmp_path: Any) -> None:
        """A stop that left the pool open would leak one per start, and a process
        an operator start/stopped all day would accumulate them silently."""
        sup = _supervisor(tmp_path)

        async def go() -> bool:
            await sup.start(Mode.V1)
            discovery = sup._discovery
            assert discovery is not None
            await sup.stop()
            return discovery._client.is_closed

        assert asyncio.run(go()) is True

    def test_the_stopped_runtime_is_replaced_not_re_shown(self, tmp_path: Any) -> None:
        """The stopped object still holds the run's markets and accumulators, and
        showing them under a STOPPED banner invites reading them as current."""
        sup = _supervisor(tmp_path)

        async def go() -> tuple[ArcRuntime, ArcRuntime]:
            started = await sup.start(Mode.V1)
            await sup.stop()
            return started, sup.runtime

        started, idle = asyncio.run(go())
        assert idle is not started

    def test_trading_is_disarmed_before_anything_is_cancelled(self, tmp_path: Any) -> None:
        """Between the cancel and the last loop pass the runtime can still reach
        `_submit_pending`. An intent submitted during a teardown is an order placed
        by the act of stopping."""
        sup = _supervisor(tmp_path)

        async def go() -> tuple[bool, bool]:
            run = await sup.start(Mode.V1)
            run.arm()
            armed_while_running = run.state.execution_armed
            await sup.stop()
            return armed_while_running, run.state.execution_armed

        armed, still_armed = asyncio.run(go())
        assert armed is True
        assert still_armed is False


class TestIsolation:
    """No object may cross a stop. The rule the specification lists item by item."""

    def test_two_starts_share_no_executor_feed_state_or_client(self, tmp_path: Any) -> None:
        sup = _supervisor(tmp_path)

        async def go() -> tuple[ArcRuntime, ArcRuntime]:
            first = await sup.start(Mode.V1)
            await sup.stop()
            second = await sup.start(Mode.V1)
            await sup.stop()
            return first, second

        first, second = asyncio.run(go())
        assert first is not second
        assert first.executor is not second.executor
        assert first.feed is not second.feed
        assert first.state is not second.state
        assert first.hub is not second.hub
        assert first.tokens is not second.tokens

    def test_switch_stops_the_previous_runtime_first(self, tmp_path: Any) -> None:
        """Same mode included: "restart V1" and "switch to V2" must be one code
        path, or a switch that skipped the teardown when the mode matched would
        keep the old feed alive."""
        sup = _supervisor(tmp_path)

        async def go() -> tuple[ArcRuntime, ArcRuntime, bool]:
            first = await sup.start(Mode.V1)
            second = await sup.switch(Mode.V1)
            running = sup.running
            await sup.stop()
            return first, second, running

        first, second, running = asyncio.run(go())
        assert second is not first
        assert running is True


class TestV2RefusesRatherThanStartsWrong:
    def test_switching_to_v2_without_credentials_names_them(self, tmp_path: Any) -> None:
        """The SDK's own failure for an empty private key is a signing error raised
        deep inside a client constructor, and an operator reading it would be
        debugging a key they never set."""
        sup = _supervisor(tmp_path)

        async def go() -> None:
            with pytest.raises(ConfigInvariantError, match="POLYMARKET_PRIVATE_KEY"):
                await sup.start(Mode.V2)

        asyncio.run(go())

    def test_the_refusal_leaves_the_process_idle_not_half_started(
        self, tmp_path: Any
    ) -> None:
        sup = _supervisor(tmp_path)

        async def go() -> tuple[bool, str]:
            with pytest.raises(ConfigInvariantError):
                await sup.start(Mode.V2)
            return sup.running, sup.status

        running, status = asyncio.run(go())
        assert running is False
        assert status == RuntimeStatus.STOPPED


@pytest.fixture
def client(tmp_path: Any) -> Iterator[TestClient]:
    """A client whose requests all share ONE event loop.

    Used as a context manager on purpose: a bare TestClient runs every request in
    a fresh loop, so the task started by the first request is already dead by the
    second and the runtime looks stopped between two clicks. The real dashboard is
    one loop for the life of the process, and the lifecycle only means anything
    when the loop outlives the request.
    """
    sup = _supervisor(tmp_path)
    with TestClient(build_app(sup.runtime, sup)) as client:
        yield client


class TestTheRoutesAreStillTwelve:
    """The lifecycle was added by REPOINTING /start and /stop, not by adding routes."""


    def test_no_route_was_added(self, client: TestClient) -> None:
        assert {r.path for r in router.routes} == {*ROUTE_PATHS, "/ws"}  # type: ignore[attr-defined]
        assert len(ROUTE_PATHS) == 12

    def test_start_and_stop_are_still_the_only_lifecycle_paths(self) -> None:
        assert "/start" in ROUTE_PATHS
        assert "/stop" in ROUTE_PATHS
        for absent in ("/runtime", "/runtime/start", "/mode", "/arm", "/trading"):
            assert absent not in ROUTE_PATHS

    def test_an_unknown_runtime_is_refused_not_defaulted(self, client: TestClient) -> None:
        """A fallback would hand the operator a runtime they did not ask for."""
        response = client.post("/start?mode=V3")
        assert response.status_code == 400
        assert "V1 or V2" in response.json()["detail"]

    def test_start_reports_that_it_armed_nothing(self, client: TestClient) -> None:
        """Stated in the response rather than left to be inferred: the operator
        pressed START and must not believe orders are now going out."""
        body = client.post("/start?mode=V1").json()
        assert body["status"] == RuntimeStatus.RUNNING_V1
        assert body["mode"] == "V1"
        assert body["execution_armed"] is False
        client.post("/stop")

    def test_a_second_start_is_a_conflict_not_a_restart(self, client: TestClient) -> None:
        client.post("/start?mode=V1")
        response = client.post("/start?mode=V1")
        assert response.status_code == 409
        assert "already running" in response.json()["detail"]
        client.post("/stop")

    def test_stop_returns_the_idle_state(self, client: TestClient) -> None:
        client.post("/start?mode=V1")
        assert client.post("/stop").json()["status"] == RuntimeStatus.STOPPED

    def test_the_routes_follow_the_supervisor_to_the_new_runtime(
        self, tmp_path: Any
    ) -> None:
        """A reference captured at mount time would keep serving the stopped run's
        state to every panel after a restart."""
        sup = _supervisor(tmp_path)
        with TestClient(build_app(sup.runtime, sup)) as client:
            mounted = sup.runtime
            client.post("/start?mode=V1")
            assert sup.runtime is not mounted
            body = client.get("/status").json()
            assert body["runtime"]["status"] == RuntimeStatus.RUNNING_V1
            client.post("/stop")


class TestTradingIsTheOtherButton:
    """START TRADING lives on the Limit Order Engine, which is where it belongs."""


    def test_arming_is_a_separate_act_from_starting(self, client: TestClient) -> None:
        client.post("/start?mode=V1")
        assert client.get("/status").json()["runtime"]["execution_armed"] is False
        armed = client.post("/strategies/arc_twap_locked_buffer/config?action=arm").json()
        assert armed["execution_armed"] is True
        client.post("/stop")

    def test_disarming_leaves_the_runtime_up(self, client: TestClient) -> None:
        """STOP TRADING stops NEW submissions and nothing else. A kill switch that
        also tore down the observation stack would blind the operator at exactly
        the moment they reached for it."""
        client.post("/start?mode=V1")
        client.post("/strategies/arc_twap_locked_buffer/config?action=arm")
        body = client.post("/strategies/arc_twap_locked_buffer/config?action=disarm").json()
        assert body["execution_armed"] is False
        assert client.get("/status").json()["runtime"]["status"] == RuntimeStatus.RUNNING_V1
        client.post("/stop")
