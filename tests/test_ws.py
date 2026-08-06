"""The `/ws` contract: replay on connect, no duplicates, no leaked subscribers.

Each failure pinned here looks like a working dashboard. A duplicated event reads as
two fills of one order. A leaked subscriber queue is a memory leak that only shows
after days of uptime. A socket that closes without unsubscribing leaves the runtime
broadcasting into nothing forever.
"""

from __future__ import annotations

import io
import logging
from typing import Any

import pytest
from conftest import VALID_TRADING_VALUES
from fastapi.testclient import TestClient

from arc.api.app import build_app
from arc.clock import FrozenClock
from arc.config import ArcSettings, Settings, build_trading_config
from arc.execution.v1_paper import PaperExecutor
from arc.market.feed import RtdsFeed
from arc.runtime.engine import ArcRuntime
from arc.runtime.state import RuntimeState
from arc.storage.store import Store

_NOW = 1_754_400_000.0


@pytest.fixture
def run(tmp_path: Any) -> ArcRuntime:
    store = Store(f"{tmp_path}/arc.db")
    store.migrate(_NOW)
    clock = FrozenClock(_NOW)
    runtime = RuntimeState(store, clock)
    runtime.load()
    return ArcRuntime(
        settings=Settings(
            env=ArcSettings(),
            trading=build_trading_config(dict(VALID_TRADING_VALUES)),
            seeded_from_env=False,
        ),
        store=store,
        clock=clock,
        runtime=runtime,
        discovery=None,  # type: ignore[arg-type]
        feed=RtdsFeed(clock),
        executor=PaperExecutor(),
        out=io.StringIO(),
        logger=logging.getLogger("arc.test.ws"),
    )


@pytest.fixture
def client(run: ArcRuntime) -> TestClient:
    return TestClient(build_app(run))


def _drain(socket: Any, count: int) -> list[dict[str, Any]]:
    return [socket.receive_json() for _ in range(count)]


class TestBacklogReplay:
    def test_events_logged_before_connect_are_replayed(
        self, client: TestClient, run: ArcRuntime
    ) -> None:
        """A blank console after a reconnect reads as a quiet runtime, not a lost one."""
        run.hub.emit("Window Engine", "INFO", "window_open", "w=0", _NOW)
        run.hub.emit("Limit Order Engine", "INFO", "order_submitted", "id=1", _NOW)
        with client.websocket_connect("/ws") as socket:
            first, second = _drain(socket, 2)
        assert [m["type"] for m in (first, second)] == ["signal", "signal"]
        assert first["data"]["event"] == "window_open"
        assert second["data"]["event"] == "order_submitted"
        assert first["data"]["seq"] < second["data"]["seq"]

    def test_a_status_frame_follows_the_replay(self, client: TestClient, run: ArcRuntime) -> None:
        run.hub.emit("Runtime", "INFO", "started", "", _NOW)
        with client.websocket_connect("/ws") as socket:
            _drain(socket, 1)
            frame = socket.receive_json()
        assert frame["type"] == "status"
        assert frame["data"]["runtime"]["status"] == "STOPPED"


class TestNoDuplicateEvents:
    def test_a_replayed_event_is_not_pushed_again(
        self, client: TestClient, run: ArcRuntime
    ) -> None:
        """Subscribe-then-replay overlaps by design; the seq floor removes the overlap."""
        run.hub.emit("Market Engine", "INFO", "rotated", "", _NOW)
        with client.websocket_connect("/ws") as socket:
            replayed = socket.receive_json()
            run.hub.emit("Market Engine", "INFO", "ptb_frozen", "", _NOW)
            frames = [socket.receive_json() for _ in range(4)]
        signals = [f for f in frames if f["type"] == "signal"]
        seqs = [replayed["data"]["seq"], *[s["data"]["seq"] for s in signals]]
        assert len(seqs) == len(set(seqs))
        assert "ptb_frozen" in [s["data"]["event"] for s in signals]

    def test_live_events_arrive_without_waiting_for_a_status_frame(
        self, client: TestClient, run: ArcRuntime
    ) -> None:
        with client.websocket_connect("/ws") as socket:
            socket.receive_json()  # the first status frame
            run.hub.emit("Limit Order Engine", "WARNING", "buffer_not_satisfied", "", _NOW)
            frame = socket.receive_json()
        assert frame["type"] == "signal"
        assert frame["data"]["severity"] == "WARNING"


class TestSubscriberLifetime:
    def test_disconnect_unsubscribes(self, client: TestClient, run: ArcRuntime) -> None:
        """A queue left in the hub is broadcast into forever: an unbounded leak."""
        assert run.hub.subscriber_count == 0
        with client.websocket_connect("/ws") as socket:
            socket.receive_json()
            assert run.hub.subscriber_count == 1
        assert run.hub.subscriber_count == 0

    def test_repeated_reconnects_do_not_accumulate(
        self, client: TestClient, run: ArcRuntime
    ) -> None:
        for _ in range(5):
            with client.websocket_connect("/ws") as socket:
                socket.receive_json()
        assert run.hub.subscriber_count == 0


class TestStatusFrameIsTheOneDocument:
    def test_the_socket_sends_the_same_document_as_the_rest_route(
        self, client: TestClient
    ) -> None:
        """OPS Deck, Signal Tank, Ledger and System must never disagree."""
        rest = client.get("/status").json()
        with client.websocket_connect("/ws") as socket:
            frame = socket.receive_json()
        assert frame["data"].keys() == rest.keys()

    def test_decimals_in_the_frame_are_strings(self, client: TestClient) -> None:
        with client.websocket_connect("/ws") as socket:
            frame = socket.receive_json()
        settings = frame["data"]["settings"]
        assert all(isinstance(v, str) for v in settings["buffers"].values())
