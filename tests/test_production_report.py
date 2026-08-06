"""The validation tooling as an operator actually reaches it: over `/history`.

`tests/test_runtime_validation.py` tests the validator against a Store. This tests
the one thing that file cannot: that the report is reachable at all, and reachable
without a thirteenth route.

The failures pinned here are the ones that leave an operator with no verdict. A
`?format=report` that returns JSON is a report nobody can read over SSH; a
`?validate=1` that quietly drops the summary is a green ledger page with no
validation on it; a validation route that appeared alongside the twelve is the
diagnostics endpoint A15 forbids. None of them raise an error.

No network: the runtime is built against a scripted discovery source and the paper
executor, exactly as `test_api.py` does it.
"""

from __future__ import annotations

import asyncio
import io
import logging
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from conftest import OFFSETS, VALID_TRADING_VALUES
from execution_fixtures import fill_engine, intent_for, make_market, submitter
from fastapi.testclient import TestClient

from arc.api.app import build_app
from arc.api.routes import ROUTE_PATHS
from arc.clock import FrozenClock
from arc.config import ArcSettings, Settings, build_trading_config
from arc.domain.enums import Direction, MarketPhase, Outcome, WindowState
from arc.domain.models import ExecutionWindow, Fill, Settlement
from arc.domain.timing import MARKET_DURATION_SECONDS, slug_for
from arc.execution.v1_paper import PaperExecutor
from arc.market.discovery import MarketMetadata
from arc.market.feed import RtdsFeed
from arc.runtime.engine import ArcRuntime
from arc.runtime.report import render_report
from arc.runtime.state import RuntimeState
from arc.runtime.validation import UNVERIFIED, validate_run
from arc.storage.store import Store

FIRST_TS = 1_754_400_000
PTB = Decimal("64000.00")
PRICE = Decimal("0.70")
SIZE = Decimal("35")


# The metrics block reports how long this run and this validation took, so two
# renderings of the same rows differ in those figures by design. Everything else
# must match exactly — the comparisons below blank the clocks and nothing more.
_VOLATILE: tuple[str, ...] = (
    "runtime_uptime_seconds",
    "validation_duration_seconds",
    "runtime uptime (s)",
    "validation duration (s)",
)


def _stable_json(payload: dict[str, Any]) -> dict[str, Any]:
    metrics = payload.get("metrics")
    if not metrics:
        return payload
    return {**payload, "metrics": {**metrics, **{k: None for k in _VOLATILE if k in metrics}}}


def _stable_text(text: str) -> str:
    return "\n".join(
        line for line in text.splitlines() if not any(v in line for v in _VOLATILE)
    )


class _Discovery:
    """Metadata that never publishes. Nothing here needs the venue."""

    async def fetch_metadata(self, slug: str) -> MarketMetadata:
        window_ts = int(slug.rsplit("-", 1)[1])
        return MarketMetadata(
            slug=slug,
            condition_id=f"0x{window_ts}",
            token_ids=("up", "down"),
            venue_close_ts=window_ts + MARKET_DURATION_SECONDS,
            ptb_raw=None,
            final_price_raw=None,
            active=True,
            closed=False,
            raw={},
        )


def _record_market(store: Store, window_ts: int, *, fire: tuple[int, ...] = (15,)) -> str:
    """One market as a real run leaves it, written through the Store's own methods."""
    slug = slug_for(window_ts)
    make_market(store, window_ts)
    store.save_ptb(slug, PTB, float(window_ts))
    store.save_accumulator(
        slug,
        running_sum=Decimal("64100.00") * 3,
        observation_count=3,
        now=float(window_ts + 200),
    )
    store.save_settlement_twap(slug, Decimal("64150.00"), float(window_ts + 300))

    for offset in OFFSETS:
        fired = offset in fire
        window = ExecutionWindow(
            offset_seconds=offset,
            state=WindowState.FIRED if fired else WindowState.EXPIRED,
            opening_twap=Decimal("64050.00"),
            ptb=PTB,
            buffer=Decimal("2.00"),
            direction=Direction.UP,
            locked_trigger=Decimal("64052.00"),
            frozen_at=float(window_ts + 300 - offset),
            fired_at=float(window_ts + 300 - offset + 0.5) if fired else None,
        )
        store.save_window_frozen(slug, window, float(window_ts + 300 - offset))
        store.save_window_state(slug, offset, window.state, float(window_ts + 300 - offset + 0.5))

    executor = PaperExecutor()
    for offset in fire:
        intent = intent_for(
            window_ts=window_ts, offset_seconds=offset, size=SIZE, limit_price=PRICE
        )
        store.save_intent(intent)  # write-before-act (A4)
        asyncio.run(
            submitter(store, executor).submit(
                intent,
                count=1,
                phase=MarketPhase.ACTIVE,
                now=float(window_ts + 300 - offset + 0.5),
            )
        )
        (order,) = (o for o in store.orders_for(slug) if o.offset_seconds == offset)
        fill_engine(store, executor).ingest(
            slug,
            (
                Fill(
                    fill_id=f"{slug}:{offset}:fill",
                    order_id=order.order_id,
                    market_slug=slug,
                    price=PRICE,
                    size=SIZE,
                    ts=float(window_ts + 300 - offset + 0.8),
                ),
            ),
            float(window_ts + 300 - offset + 0.8),
        )

    store.save_settlement(
        Settlement(
            market_slug=slug,
            outcome=Outcome.UP,
            settlement_twap=Decimal("64150.00"),
            ptb=PTB,
            settled_at=float(window_ts + 325),
        )
    )
    store.save_phase(slug, MarketPhase.SETTLED, float(window_ts + 325))
    return slug


@pytest.fixture
def run(tmp_path: Path) -> ArcRuntime:
    store = Store(f"{tmp_path}/arc.db")
    store.migrate(float(FIRST_TS))
    for i in range(3):
        _record_market(store, FIRST_TS + i * MARKET_DURATION_SECONDS)
    clock = FrozenClock(float(FIRST_TS + 4 * MARKET_DURATION_SECONDS))
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
        logger=logging.getLogger("arc.test.report"),
    )


@pytest.fixture
def client(run: ArcRuntime) -> TestClient:
    return TestClient(build_app(run))


class TestTheReportHasNoRouteOfItsOwn:
    """A15/Q2: twelve routes. The validation summary is a parameter, not a route."""

    def test_no_validation_route_was_added(self, client: TestClient) -> None:
        for path in ("/validate", "/validation", "/report", "/production", "/diagnostics"):
            assert client.get(path).status_code == 404, path

    def test_the_route_list_is_still_the_twelve(self) -> None:
        assert len(ROUTE_PATHS) == 12
        assert "/history" in ROUTE_PATHS

    def test_history_without_parameters_carries_no_validation_key(
        self, client: TestClient
    ) -> None:
        """The ledger page is not the validation page. An operator loading history
        should not be paying for a hundred-market audit on every render."""
        body = client.get("/history").json()
        assert "validation" not in body
        assert body["records"]


class TestValidateAttachesTheSummary:
    def test_validate_adds_the_validation_block(self, client: TestClient) -> None:
        body = client.get("/history?validate=1").json()
        assert "validation" in body

    def test_the_records_are_still_there(self, client: TestClient) -> None:
        """?validate=1 augments the ledger; it does not replace it."""
        plain = client.get("/history").json()["records"]
        both = client.get("/history?validate=1").json()["records"]
        assert both == plain

    def test_the_summary_is_json_serialisable(self, client: TestClient) -> None:
        response = client.get("/history?validate=1")
        assert response.status_code == 200
        assert isinstance(response.json()["validation"], dict)

    def test_a_three_market_run_is_not_ready_for_live(self, client: TestClient) -> None:
        """Three markets is not a hundred, and the route must not soften that."""
        assert client.get("/history?validate=1").json()["validation"]["ready_for_live"] is False

    def test_the_operator_criteria_stay_unverified_over_the_route(
        self, client: TestClient
    ) -> None:
        criteria = client.get("/history?validate=1").json()["validation"]["criteria"]
        assert any(c["result"] == UNVERIFIED for c in criteria)


class TestFormatReportIsText:
    def test_the_response_is_plain_text_not_json(self, client: TestClient) -> None:
        response = client.get("/history?format=report")
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/plain")

    def test_it_reads_as_the_report(self, client: TestClient) -> None:
        body = client.get("/history?format=report").text
        assert "ARC — PRODUCTION VALIDATION REPORT" in body
        assert "ALL CRITERIA" in body

    def test_the_verdict_is_stated_and_is_not_ready(self, client: TestClient) -> None:
        assert "NOT READY" in client.get("/history?format=report").text

    def test_the_report_names_the_mode_and_the_provider(
        self, client: TestClient, run: ArcRuntime
    ) -> None:
        """Rendered from the runtime that produced the rows. A report that did not
        say which mode and which provider produced it is evidence of nothing."""
        body = client.get("/history?format=report").text
        assert run.mode.value in body
        assert run.settings.env.twap_provider in body

    def test_the_host_metrics_are_declared_unmeasured(self, client: TestClient) -> None:
        """Criterion 13's other half. ARC does not sample CPU or memory, and a
        number printed here would be read as a measurement."""
        assert "NOT MEASURED BY ARC" in client.get("/history?format=report").text

    def test_csv_still_wins_when_asked_for(self, client: TestClient) -> None:
        """The three formats do not collide."""
        assert client.get("/history?format=csv").headers["content-type"].startswith("text/csv")


class TestTheRouteReadsTheSameRowsTheValidatorDoes:
    def test_the_route_summary_matches_a_direct_validation(
        self, client: TestClient, run: ArcRuntime
    ) -> None:
        """No second implementation. If the route computed its own answer it could
        disagree with `arc validate`, and the operator would have two verdicts."""
        direct = validate_run(
            run.store,
            offsets=tuple(run.settings.trading.windows_by_priority),
            cadence_seconds=MARKET_DURATION_SECONDS,
            market_limit=50,
        )
        over_the_route = client.get("/history?validate=1").json()["validation"]
        assert _stable_json(over_the_route) == _stable_json(direct.as_json())

    def test_the_rendered_text_matches_render_report(
        self, client: TestClient, run: ArcRuntime
    ) -> None:
        direct = validate_run(
            run.store,
            offsets=tuple(run.settings.trading.windows_by_priority),
            cadence_seconds=MARKET_DURATION_SECONDS,
            market_limit=50,
        )
        expected = render_report(
            direct, mode=run.mode.value, provider=run.settings.env.twap_provider
        )
        assert _stable_text(client.get("/history?format=report").text) == _stable_text(
            expected
        )

    def test_the_market_limit_reaches_the_validator(self, client: TestClient) -> None:
        """`?markets=` bounds the audit as well as the ledger, so a report and the
        rows under it describe the same window of the run."""
        one = client.get("/history?validate=1&markets=1").json()["validation"]
        three = client.get("/history?validate=1&markets=3").json()["validation"]
        assert one["recorder"]["markets_audited"] == 1
        assert three["recorder"]["markets_audited"] == 3


class TestTheReportIsReadOnly:
    def test_rendering_it_writes_nothing(self, client: TestClient, run: ArcRuntime) -> None:
        """The validator audits; it must not repair. A missing value that the act of
        reporting filled in would make the report describe itself."""
        before = [dict(r) for r in run.store.recent_markets(limit=50)]
        client.get("/history?format=report")
        client.get("/history?validate=1")
        after = [dict(r) for r in run.store.recent_markets(limit=50)]
        assert after == before

    def test_two_reads_agree(self, client: TestClient) -> None:
        first = _stable_text(client.get("/history?format=report").text)
        assert _stable_text(client.get("/history?format=report").text) == first


class TestAnEmptyRunReportsHonestly:
    """The dangerous case: a validator that passes because nothing happened."""

    @pytest.fixture
    def empty(self, tmp_path: Path) -> TestClient:
        store = Store(f"{tmp_path}/empty.db")
        store.migrate(float(FIRST_TS))
        clock = FrozenClock(float(FIRST_TS))
        runtime = RuntimeState(store, clock)
        runtime.load()
        return TestClient(
            build_app(
                ArcRuntime(
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
                    logger=logging.getLogger("arc.test.report.empty"),
                )
            )
        )

    def test_no_rows_is_still_a_two_hundred(self, empty: TestClient) -> None:
        assert empty.get("/history?format=report").status_code == 200

    def test_no_rows_is_never_ready_for_live(self, empty: TestClient) -> None:
        assert empty.get("/history?validate=1").json()["validation"]["ready_for_live"] is False

    def test_the_empty_report_says_not_ready(self, empty: TestClient) -> None:
        assert "NOT READY" in empty.get("/history?format=report").text


def test_the_report_is_reachable_without_credentials(client: TestClient) -> None:
    """A4: the loopback bind is the access control. No header is required, and no
    header would be accepted as one either — there is nothing to accept it."""
    response: Any = client.get("/history?format=report")
    assert response.status_code == 200
