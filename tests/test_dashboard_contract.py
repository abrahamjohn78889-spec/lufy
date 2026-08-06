"""DASHBOARD CONTRACT: one document, one state, and no hidden runtime.

The three contracts in Part 2 that nothing else pins. Each has a failure the
operator would never see as an error:

  Decimal — a money value serialized as a JSON number is silent precision loss;
            0.1 + 0.2 does not come back out as 0.3.
  WebSocket — a panel that fetches its own slice shows a different instant, and a
            market boundary landing between two fetches renders one market's PTB
            against another's TWAP.
  Runtime  — a status value the markup cannot render is a state the operator sees
            as blank, which reads as a dashboard bug rather than as the state.
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import re
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from conftest import VALID_TRADING_VALUES

from arc.api.models import LOE_STAGES, status_payload
from arc.api.ws import STATUS_INTERVAL
from arc.clock import FrozenClock
from arc.config import ArcSettings, Settings, build_trading_config
from arc.domain.enums import DISPLAYED_ORDER_STATES, DenialReason
from arc.execution.v1_paper import PaperExecutor
from arc.market.feed import RtdsFeed
from arc.runtime.engine import ArcRuntime, RuntimeStatus
from arc.runtime.state import RuntimeState
from arc.storage.store import Store

_WEB = Path(__file__).resolve().parent.parent / "arc" / "web"
_APP_JS = (_WEB / "app.js").read_text(encoding="utf-8")
_INDEX = (_WEB / "index.html").read_text(encoding="utf-8")
_NOW = 1_754_400_000.0

# Keys that are genuinely counts, timestamps or flags, not money. A count rendered
# as a string would be the opposite error: "12" sorting before "9" in the ledger.
_NOT_MONEY = re.compile(
    r"(^ts$|_count|_seconds|_ms|_ts|_at|_samples|_processed|_submitted|_repriced"
    r"|_recorded|_accepted|_rejected|_frozen|_unavailable|_tables|_gb|reconnects"
    r"|seq|version)$"
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
        discovery=None,  # type: ignore[arg-type]
        feed=RtdsFeed(clock),
        executor=PaperExecutor(),
        out=io.StringIO(),
        logger=logging.getLogger("arc.test.contract"),
    )


def _doc(run: ArcRuntime) -> dict[str, Any]:
    return asyncio.run(status_payload(run, _NOW))


def _walk(node: Any, path: str = "") -> list[tuple[str, Any]]:
    if isinstance(node, dict):
        return [p for k, v in node.items() for p in _walk(v, f"{path}.{k}" if path else k)]
    if isinstance(node, list):
        return [p for i, v in enumerate(node) for p in _walk(v, f"{path}[{i}]")]
    return [(path, node)]


class TestDecimalContract:
    def test_no_decimal_object_survives_serialization(self, run: ArcRuntime) -> None:
        """A Decimal reaching json.dumps raises. The API must never get that far."""
        for path, value in _walk(_doc(run)):
            assert not isinstance(value, Decimal), path

    def test_the_whole_document_is_json_serializable(self, run: ArcRuntime) -> None:
        json.dumps(_doc(run))

    def test_no_money_field_is_a_json_number(self, run: ArcRuntime) -> None:
        """Every Decimal crossing the boundary is a STRING. Never a JSON number."""
        bad = [
            path
            for path, value in _walk(_doc(run))
            if isinstance(value, float) and not _NOT_MONEY.search(path.split(".")[-1])
        ]
        assert bad == [], bad

    def test_counts_are_not_stringified(self, run: ArcRuntime) -> None:
        """The reverse error: "12" sorts before "9" and the operator reads it wrong."""
        doc = _doc(run)
        for key, value in doc["stats"].items():
            assert isinstance(value, bool | int), (key, value)

    def test_the_browser_never_reformats_a_string_value(self) -> None:
        """text() assigns verbatim. Any reformatting would be a second rounding."""
        body = _APP_JS.split("function text(", 1)[1].split("\n}", 1)[0]
        assert "String(value)" in body
        assert "toFixed" not in body and "Number" not in body


class TestOneDocumentOneState:
    def test_every_workspace_reads_the_same_state_object(self) -> None:
        """A second fetch per panel is a second instant, and the panels disagree."""
        assert _APP_JS.count("let state = null") == 1
        # The binder reads `state` and nothing else; each painter takes its slice
        # from the same object rather than requesting one.
        for painter in ("paintEngines", "paintPreflight", "paintLoe", "paintWindows",
                        "paintAnalytics", "paintSettings"):
            body = _APP_JS.split(f"function {painter}()", 1)[1].split("\nfunction ", 1)[0]
            assert "fetch(" not in body, painter

    def test_the_status_document_carries_every_required_section(self, run: ArcRuntime) -> None:
        doc = _doc(run)
        assert set(doc) >= {
            "ts", "runtime", "engines", "market", "derived", "execution", "wallet",
            "recovery", "stats", "preflight", "settings", "system",
        }

    def test_the_socket_pushes_the_whole_document_not_a_diff(self) -> None:
        """A diff protocol means two implementations of the state, one in each half."""
        assert "frame.data" in _APP_JS
        assert "state = frame.data" in _APP_JS

    def test_the_push_interval_is_human_readable_and_not_a_poll(self) -> None:
        assert 0.5 <= STATUS_INTERVAL <= 2.0


class TestNoHiddenRuntimeState:
    def test_the_status_is_one_of_the_five(self, run: ArcRuntime) -> None:
        """Q4: five values, nothing else. No observation state anywhere."""
        allowed = {
            RuntimeStatus.STOPPED, RuntimeStatus.STARTING, RuntimeStatus.RUNNING_V1,
            RuntimeStatus.RUNNING_V2, RuntimeStatus.STOPPING,
        }
        assert _doc(run)["runtime"]["status"] in allowed

    def test_observation_mode_is_gone_from_the_dashboard(self) -> None:
        """Q4 removed the third mode. A leftover label would offer a mode that is gone."""
        for text in (_APP_JS, _INDEX):
            lowered = text.lower()
            assert "observe" not in lowered
            assert "observation mode" not in lowered
            assert "observation only" not in lowered

    def test_paused_and_recovering_are_both_renderable(self, run: ArcRuntime) -> None:
        """Named runtime contracts. A state with no field is a blank the operator reads
        as a broken panel."""
        assert 'data-f="runtime.status"' in _INDEX
        assert 'data-f="recovery.running"' in _INDEX
        # Pause is the third flag, independent of both gates.
        assert 'data-f="runtime.paused"' in _INDEX
        assert isinstance(_doc(run)["runtime"]["paused"], bool)

    def test_trading_disabled_and_enabled_are_distinct_fields(self, run: ArcRuntime) -> None:
        rt = _doc(run)["runtime"]
        assert isinstance(rt["trading_enabled"], bool)
        assert isinstance(rt["execution_armed"], bool)


class TestRejectionIsSeparateFromState:
    def test_the_displayed_states_do_not_include_a_reason(self) -> None:
        """A reason folded into the state list would make "why" unreadable."""
        for reason in DenialReason:
            assert reason.value not in DISPLAYED_ORDER_STATES

    def test_buffer_not_satisfied_is_neither_a_rejection_nor_a_fill(self) -> None:
        assert "BUFFER_NOT_SATISFIED" in LOE_STAGES
        assert "BUFFER_NOT_SATISFIED" not in DISPLAYED_ORDER_STATES

    def test_the_ledger_renders_state_and_reason_in_separate_columns(self) -> None:
        columns = _APP_JS.split("LEDGER_COLUMNS = [", 1)[1].split("]", 1)[0]
        assert "'state_display'" in columns
        assert "'rejection_display'" in columns


class TestTransparency:
    def test_every_backend_severity_has_a_row_class(self) -> None:
        """An unstyled severity renders as a plain row and a fatal reads as routine."""
        css = (_WEB / "style.css").read_text(encoding="utf-8")
        for severity in ("fatal", "error", "warning"):
            assert f"tr.{severity}" in css, severity

    def test_the_tank_is_the_only_place_events_are_rendered(self) -> None:
        assert _APP_JS.count("function addEvent(") == 1

    def test_an_error_counter_can_be_traced_to_its_event(self) -> None:
        """A count with no way to reach the event sends the operator to the log file."""
        assert "el.dataset.seq" in _APP_JS
        assert "show('tank')" in _APP_JS
