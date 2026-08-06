"""The API contract: exactly twelve routes, no /health, no auth, loopback only.

Each failure pinned here is one an operator could not see. A thirteenth route is
surface nobody guards; a JSON number is a silent precision loss on a locked
trigger; a non-loopback bind is a trading control panel on the public internet with
no password. None of those show up as an error at runtime.

No network: the runtime is built against a scripted discovery source and the paper
executor, so the whole contract is checked without venue credentials.
"""

from __future__ import annotations

import io
import logging
import re
from pathlib import Path
from typing import Any

import pytest
from conftest import VALID_TRADING_VALUES
from fastapi.testclient import TestClient

import arc.api
from arc.api.app import build_app, check_bind
from arc.api.routes import ROUTE_PATHS, router
from arc.clock import FrozenClock
from arc.config import ArcSettings, Settings, build_trading_config
from arc.domain.enums import Direction
from arc.errors import ArcFatalError
from arc.execution.v1_paper import PaperExecutor
from arc.market.discovery import MarketMetadata
from arc.market.feed import RtdsFeed
from arc.runtime.engine import ArcRuntime
from arc.runtime.state import RuntimeState
from arc.storage.store import Store

_NOW = 1_754_400_000.0


class _Discovery:
    """Metadata that never publishes. The API must render UNAVAILABLE, not guess."""

    async def fetch_metadata(self, slug: str) -> MarketMetadata:
        window_ts = int(slug.rsplit("-", 1)[1])
        return MarketMetadata(
            slug=slug,
            condition_id=f"0x{window_ts}",
            token_ids=("up", "down"),
            venue_close_ts=window_ts + 300,
            ptb_raw=None,
            final_price_raw=None,
            active=True,
            closed=False,
            raw={},
        )


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
        discovery=_Discovery(),  # type: ignore[arg-type]
        feed=RtdsFeed(clock),
        executor=PaperExecutor(),
        out=io.StringIO(),
        logger=logging.getLogger("arc.test.api"),
    )


@pytest.fixture
def client(run: ArcRuntime) -> TestClient:
    return TestClient(build_app(run))


def _paths(client: TestClient) -> set[str]:
    """Every mounted API path.

    Read off the router rather than `app.routes`: FastAPI includes routers lazily, so
    the app exposes an opaque wrapper until the first request resolves. The router is
    the real surface, and the static mount and index document add none of it.
    """
    del client
    return {route.path for route in router.routes}  # type: ignore[attr-defined]


class TestExactlyTwelveRoutes:
    def test_twelve_rest_routes_and_the_websocket(self, client: TestClient) -> None:
        """A route not in ROUTE_PATHS is a route no test guards."""
        assert _paths(client) == {*ROUTE_PATHS, "/ws"}
        assert len(ROUTE_PATHS) == 12

    def test_there_is_no_health_endpoint(self, client: TestClient) -> None:
        """PM2 restarts the process. A /health route's only reader is a scanner."""
        assert client.get("/health").status_code == 404
        assert "/health" not in _paths(client)

    def test_no_debug_admin_or_export_routes(self, client: TestClient) -> None:
        for path in ("/backup", "/export", "/debug", "/admin", "/metrics", "/docs", "/openapi.json"):
            assert client.get(path).status_code == 404, path


class TestNoAccessControlCode:
    def test_no_middleware_is_installed(self, run: ArcRuntime) -> None:
        """The loopback bind IS the access control (A3/A4). Middleware here is a smell."""
        app = build_app(run)
        assert app.user_middleware == []

    def test_every_route_answers_without_credentials(self, client: TestClient) -> None:
        assert client.get("/status").status_code == 200
        assert client.get("/settings").status_code == 200
        assert client.get("/strategies").status_code == 200

    def test_the_forbidden_words_do_not_appear_in_the_package(self) -> None:
        """A3 is a grep, so it is enforced as one.

        Prose counts: a docstring explaining why there is no sign-in is how the
        pattern gets copied back in, and the audit that greps this package cannot
        tell a comment from a code path.
        """
        pattern = re.compile(r"auth|jwt|login|session|rbac", re.IGNORECASE)
        package = Path(arc.api.__file__).parent
        for path in sorted(package.glob("*.py")):
            hits = [
                f"{path.name}:{n}"
                for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
                if pattern.search(line)
            ]
            assert hits == [], hits


class TestLoopbackOnly:
    def test_non_loopback_bind_is_refused(self) -> None:
        """A bind that warned instead of raising would expose the panel silently."""
        for host in ("0.0.0.0", "192.168.1.10", "::"):
            with pytest.raises(ArcFatalError):
                check_bind(host)

    def test_loopback_is_accepted(self) -> None:
        assert check_bind("127.0.0.1") == "127.0.0.1"
        assert check_bind("::1") == "::1"

    def test_a_hostname_is_refused(self) -> None:
        with pytest.raises(ArcFatalError):
            check_bind("localhost")


def _numbers(value: Any, path: str = "", *, money: tuple[str, ...]) -> list[str]:
    """Every money-named key whose value came back as a JSON number."""
    bad: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            here = f"{path}.{key}"
            if key in money and isinstance(item, (int, float)) and not isinstance(item, bool):
                bad.append(here)
            bad += _numbers(item, here, money=money)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            bad += _numbers(item, f"{path}[{index}]", money=money)
    return bad


_MONEY = (
    "ptb", "signal_twap", "settlement_twap", "locked_trigger", "buffer", "order_price",
    "fill_price", "quantity", "filled_quantity", "remaining_quantity", "pnl",
    "opening_twap", "configured_buffer", "implied_btc_move", "tick_size",
    "min_tradable_size", "entry_price_min", "entry_price_max", "position_notional_usd",
    "max_daily_loss_usd", "current_exposure", "available_balance", "buying_power",
    "best_bid", "passive_limit", "realized_today", "realized_lifetime",
)


class TestDecimalContract:
    def test_status_serialises_every_decimal_as_a_string(self, client: TestClient) -> None:
        """A JSON number is an IEEE double and the precision loss is invisible on screen."""
        payload = client.get("/status").json()
        assert _numbers(payload, money=_MONEY) == []

    def test_settings_and_history_serialise_decimals_as_strings(self, client: TestClient) -> None:
        assert _numbers(client.get("/settings").json(), money=_MONEY) == []
        assert _numbers(client.get("/history").json(), money=_MONEY) == []
        assert _numbers(
            client.get("/strategies/arc_twap_locked_buffer/config").json(), money=_MONEY
        ) == []


class TestBothGatesAreIndependent:
    """The operator gate lives on the Limit Order Engine's config route.

    Not on `/start`: that boots the runtime and deliberately arms nothing, so the
    two gates are tested where the operator actually presses them.
    """

    ARM = "/strategies/arc_twap_locked_buffer/config?action=arm"
    DISARM = "/strategies/arc_twap_locked_buffer/config?action=disarm"

    def test_arming_cannot_open_a_closed_system_gate(
        self, client: TestClient, run: ArcRuntime
    ) -> None:
        """State 4: the operator can never override the system flag (Q1).

        Arming is allowed to succeed; what must not happen is a submission. The
        system gate is checked at decision time, so an armed operator over a
        disabled system still trades nothing.
        """
        run.state.disable_trading("SPEC_UNVERIFIED")
        client.post(self.ARM)
        assert run.state.trading_enabled is False
        assert run.state.gate.reason == "SPEC_UNVERIFIED"

    def test_disarming_does_not_touch_the_system_gate(
        self, client: TestClient, run: ArcRuntime
    ) -> None:
        run.arm()
        body = client.post(self.DISARM).json()
        assert body["execution_armed"] is False
        assert body["trading_enabled"] is run.state.trading_enabled

    def test_pause_and_resume_do_not_clear_the_arm_state(
        self, client: TestClient, run: ArcRuntime
    ) -> None:
        """Pausing must not require a second confirmation to resume."""
        run.arm()
        assert client.post("/pause").json() == {"execution_armed": True, "paused": True}
        assert client.post("/resume").json() == {"execution_armed": True, "paused": False}

    def test_status_reports_both_flags_separately(self, client: TestClient) -> None:
        runtime = client.get("/status").json()["runtime"]
        assert "trading_enabled" in runtime
        assert "execution_armed" in runtime


class TestConfigurationLock:
    def test_settings_cannot_be_written_while_armed(
        self, client: TestClient, run: ArcRuntime
    ) -> None:
        """A buffer edited under a resting order changes the rule it was authorised by."""
        run.arm()
        assert client.post("/settings", json={"submission_count": "2"}).status_code == 409

    def test_a_rejected_value_is_not_persisted(self, client: TestClient, run: ArcRuntime) -> None:
        before = run.store.load_settings()
        response = client.post("/settings", json={"submission_count": "0"})
        assert response.status_code == 400
        assert run.store.load_settings() == before

    def test_only_the_editable_fields_are_accepted(self, client: TestClient) -> None:
        response = client.post("/settings", json={"tick_size": "0.05"})
        assert response.status_code == 400
        assert "not editable" in response.json()["detail"]

    def test_a_valid_edit_is_saved_and_reports_the_restart(self, client: TestClient) -> None:
        body = client.post("/settings", json={"submission_count": "2"}).json()
        assert body["saved"] is True
        assert body["restart_required"] is True
        assert body["values"]["submission_count"] == "2"


class TestStrategyIsReadOnly:
    def test_only_one_strategy_exists_and_it_is_pinned(self, client: TestClient) -> None:
        entries = client.get("/strategies").json()
        assert [e["id"] for e in entries] == ["arc_twap_locked_buffer"]
        assert entries[0]["pinned"] is True
        assert entries[0]["disableable"] is False

    def test_the_parameters_are_not_writable(self, client: TestClient) -> None:
        """A17: the strategy is pinned, so the parameter write path must refuse.

        The route itself is not refused outright any more — it carries START and
        STOP TRADING — but a POST that tries to edit the strategy still 405s and
        the message says where buffers and windows are edited instead.
        """
        response = client.post("/strategies/arc_twap_locked_buffer/config")
        assert response.status_code == 405
        assert "Settings page" in response.json()["detail"]

    def test_an_unknown_strategy_is_a_404_not_a_default(self, client: TestClient) -> None:
        assert client.get("/strategies/nope").status_code == 404
        assert client.get("/strategies/nope/config").status_code == 404


class TestHistoryIsTheOnlyHistory:
    def test_csv_export_is_a_query_parameter_not_a_route(self, client: TestClient) -> None:
        response = client.get("/history", params={"format": "csv"})
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/csv")

    def test_search_is_a_query_parameter(self, client: TestClient) -> None:
        assert client.get("/history", params={"q": "nothing-like-this"}).json()["records"] == []

    def test_a_wall_clock_filter_is_read_in_the_zone_it_names(self, client: TestClient) -> None:
        """Analytics filtering by IST or ET. The bound is converted once, on the way
        in; what the store filters on is still the canonical epoch."""
        far_future = {"since": "2099-01-01 00:00:00", "tz": "ist"}
        assert client.get("/history", params=far_future).json()["records"] == []
        assert client.get("/history", params={"since": "2000-01-01", "tz": "et"}).status_code == 200

    def test_a_mistyped_filter_is_unfiltered_not_a_five_hundred(self, client: TestClient) -> None:
        assert client.get("/history", params={"since": "last tuesday"}).status_code == 200

    def test_the_csv_export_flattens_the_display_blocks(self, client: TestClient) -> None:
        """A nested dict written verbatim would land in the file as a Python repr."""
        body = client.get("/history", params={"format": "csv"}).text
        assert "{" not in body


class TestBackupIsAQueryParameter:
    def test_backup_writes_a_timestamped_local_copy(
        self, client: TestClient, run: ArcRuntime
    ) -> None:
        """Local only. No cloud storage, and no /backup route."""
        body = client.post("/settings", params={"action": "backup"}).json()
        assert body["bytes"] > 0
        assert body["backup"].startswith("arc-")
        assert client.get("/settings", params={"snapshot": "list"}).json()["snapshots"]


class TestResearchRoutes:
    def test_backtest_always_carries_the_warning(self, client: TestClient) -> None:
        """A18: a performance number here would answer a question the data cannot."""
        body = client.get("/backtest", params={"start": 0, "end": 300}).json()
        assert "Signal visualization only" in body["warning"]
        for forbidden in ("win_rate", "sharpe", "roi", "equity_curve", "drawdown"):
            assert forbidden not in body

    def test_backtest_refuses_an_inverted_range(self, client: TestClient) -> None:
        assert client.get("/backtest", params={"start": 300, "end": 0}).status_code == 400

    def test_orderbook_reads_the_executor_not_a_second_venue_call(
        self, client: TestClient, run: ArcRuntime
    ) -> None:
        run.rotator.advance(_NOW)
        market = run.rotator.current
        assert market is not None
        run.executor.quote(market.slug, Direction.UP, __import__("decimal").Decimal("0.74"))
        body = client.get("/orderbook", params={"direction": "UP"}).json()
        assert body["best_bid"] == "0.74"

    def test_orderbook_refuses_an_unknown_direction(self, client: TestClient) -> None:
        assert client.get("/orderbook", params={"direction": "SIDEWAYS"}).status_code == 400
