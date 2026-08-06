"""OPS DECK: the sixteen primary elements, the ten engine rows, the fifteen checks.

The failure this pins is the quiet one — a panel that was specified, agreed, and
then never rendered. Nothing errors; the operator simply never sees the value and
goes back to SSH for it, which is the trip the whole workspace exists to remove.

The markup half is asserted structurally (there is no JS runtime here) and the
payload half against real engine objects, so a panel cannot bind a path the
backend does not ship.
"""

from __future__ import annotations

import io
import logging
import re
from pathlib import Path
from typing import Any

import pytest
from conftest import VALID_TRADING_VALUES

from arc.api.models import (
    LOE_STAGES,
    derived_payload,
    engine_status,
    preflight,
)
from arc.clock import FrozenClock
from arc.config import ArcSettings, Settings, build_trading_config
from arc.execution.v1_paper import PaperExecutor
from arc.market.feed import RtdsFeed
from arc.runtime.engine import ArcRuntime
from arc.runtime.state import RuntimeState
from arc.storage.store import Store

_WEB = Path(__file__).resolve().parent.parent / "arc" / "web"
_INDEX = (_WEB / "index.html").read_text(encoding="utf-8")
_OPS = _INDEX.split('id="ws-ops"', 1)[1].split("</main>", 1)[0]
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
        logger=logging.getLogger("arc.test.ops"),
    )


def _bound(fragment: str) -> set[str]:
    return set(re.findall(r'data-f="([^"]+)"', fragment))


# A16's sixteen primary elements, each mapped to what actually carries it on the
# deck. Written as the mapping rather than as prose so a panel that is deleted in a
# later refactor fails here instead of being noticed by an operator months later.
_A16: dict[str, str] = {
    "Polymarket timer": 'id="timer1"',
    "Current market": 'data-f="market.slug"',
    "PTB": 'data-f="market.ptb"',
    "Current TWAP": 'data-f="market.signal_twap"',
    "Locked Trigger": 'data-f="derived.locked_trigger"',
    "Current Window": 'data-f="derived.current_window"',
    "Direction": 'data-f="derived.frozen_direction"',
    "Trigger Status": 'id="loe"',
    "Order Status": 'id="order-states"',
    "Position": 'data-f="wallet.open_position_count"',
    "Balance": 'data-f="wallet.available_balance"',
    "Running P/L": 'data-f="wallet.unrealized_pnl"',
}

# The last four are workspaces reached from the always-visible tab bar, not deck
# panels: "without switching tabs" applies to the twelve values above.
_TABS = ("ledger", "tank", "settings")


class TestSixteenPrimaryElements:
    def test_every_primary_value_is_on_the_deck(self) -> None:
        missing = [name for name, marker in _A16.items() if marker not in _OPS]
        assert missing == [], missing

    def test_history_logs_and_settings_are_one_click_from_anywhere(self) -> None:
        """The tab bar is in <header>, outside every workspace, so it never hides."""
        header = _INDEX.split("<header", 1)[1].split("</header>", 1)[0]
        for ws in _TABS:
            assert f'data-ws="{ws}"' in header

    def test_the_ops_deck_is_the_default_workspace(self) -> None:
        assert 'class="ws active" id="ws-ops"' in _INDEX
        assert _INDEX.count('class="ws active"') == 1
        assert _INDEX.count('class="tab active"') == 1

    def test_the_disable_banner_sits_above_every_workspace(self) -> None:
        """Inside a workspace it would be invisible from the other seven."""
        before_first_ws = _INDEX.split('<main class="ws', 1)[0]
        assert 'id="banner"' in before_first_ws


class TestPanelsRequiredByTheSpec:
    def test_each_named_panel_exists(self) -> None:
        for heading in (
            "Runtime", "Engines", "Runtime Control", "Trading Control", "Preflight",
            "Running TWAP", "Limit Order Engine", "Market Information", "Provider",
            "Execution", "Recovery", "Error Summary", "Runtime Counters", "Wallet",
        ):
            assert f"<h2>{heading}" in _OPS, heading

    def test_starting_the_runtime_is_a_different_button_from_trading(self) -> None:
        """The one failure this prevents: an operator who pressed START to look at
        the system and finds the next window submitted an order. The runtime panel
        must not post to the arm route, and the trading panel must not post to
        /start."""
        runtime = _OPS.split("<h2>Runtime Control", 1)[1].split("</section>", 1)[0]
        trading = _OPS.split("<h2>Trading Control", 1)[1].split("</section>", 1)[0]
        assert "START RUNTIME" in runtime
        assert "action=arm" not in runtime
        assert "START TRADING" in trading
        assert "action=arm" in trading
        assert 'data-post="/start' not in trading

    def test_the_mode_selector_selects_rather_than_starts(self) -> None:
        """A mode button that booted a live venue session on one click is a live
        session started by a misclick."""
        runtime = _OPS.split("<h2>Runtime Control", 1)[1].split("</section>", 1)[0]
        assert 'data-mode="V1"' in runtime
        assert 'data-mode="V2"' in runtime
        for line in runtime.splitlines():
            if "data-mode=" in line:
                assert "data-post=" not in line, line

    def test_the_runtime_buttons_name_the_selected_mode(self) -> None:
        """"START RUNTIME" beside a highlighted V2 reads as V1 at a glance, and the
        two differ by whether the orders are real. app.js rewrites both labels
        whenever the selection changes."""
        app = (_WEB / "app.js").read_text(encoding="utf-8")
        assert "#runtime-start" in app and "#runtime-stop" in app
        for target in ("$('#runtime-start').textContent", "$('#runtime-stop').textContent"):
            assert target in app, target
        assert 'id="runtime-stop"' in _OPS

    def test_trading_control_has_all_four_buttons(self) -> None:
        """Pause and resume belong to the Limit Order Engine, not to a general
        controls row. An operator who cannot find PAUSE reaches for STOP RUNTIME,
        which tears down the feeds instead of holding one window."""
        trading = _OPS.split("<h2>Trading Control", 1)[1].split("</section>", 1)[0]
        for label in ("START TRADING", "PAUSE TRADING", "RESUME TRADING", "STOP TRADING"):
            assert label in trading, label
        assert 'data-post="/pause"' in trading
        assert 'data-post="/resume"' in trading

    def test_both_gates_are_displayed_independently(self) -> None:
        """A single combined light would hide system-disabled-while-armed."""
        assert 'data-f="runtime.trading_enabled"' in _OPS
        assert 'data-f="runtime.execution_armed"' in _OPS

    def test_the_disable_reason_has_its_own_field(self) -> None:
        assert 'data-f="runtime.disable_reason"' in _OPS

    def test_signal_and_settlement_twap_appear_together(self) -> None:
        panel = _OPS.split("<h2>Running TWAP", 1)[1].split("</section>", 1)[0]
        assert 'data-f="market.signal_twap"' in panel
        assert 'data-f="market.settlement_twap"' in panel
        assert 'data-f="market.ptb"' in panel

    def test_wallet_is_the_last_panel_on_the_deck(self) -> None:
        assert _OPS.rindex("<h2>Wallet") > _OPS.rindex("<h2>Runtime Counters")

    def test_there_is_no_strategy_selector_anywhere(self) -> None:
        """A17: one strategy, read-only text. A dropdown implies others exist."""
        assert "<select" not in _OPS
        settings = _INDEX.split('id="ws-settings"', 1)[1].split("</main>", 1)[0]
        assert "<select" not in settings

    def test_the_provider_is_displayed_but_never_selectable(self) -> None:
        """Criterion 20: provider switching is configuration, not a runtime control."""
        assert 'data-f="settings.provider"' in _OPS
        assert "<select" not in _OPS


class TestLifecycleMarkupMatchesTheBackend:
    def test_the_nine_stages_are_rendered_in_backend_order(self) -> None:
        """A stage in the DOM that the backend never emits is a step that never lights."""
        panel = _OPS.split('id="loe"', 1)[1].split("</ol>", 1)[0]
        assert tuple(re.findall(r'data-stage="([A-Z_]+)"', panel)) == LOE_STAGES

    def test_submission_and_fill_are_separate_boxes(self) -> None:
        assert LOE_STAGES.index("ORDER_SUBMITTED") < LOE_STAGES.index("WAITING_FOR_FILL")
        assert LOE_STAGES.index("WAITING_FOR_FILL") < LOE_STAGES.index("FILLED")

    def test_buffer_not_satisfied_is_a_stage_not_an_error(self) -> None:
        assert "BUFFER_NOT_SATISFIED" in LOE_STAGES


class TestEveryBoundPathIsShipped:
    def test_no_panel_binds_a_path_the_backend_does_not_send(self, run: ArcRuntime) -> None:
        """A typo'd data-f renders an em dash forever and looks like "no data yet"."""
        import asyncio

        from arc.api.models import status_payload

        doc = asyncio.run(status_payload(run, _NOW))

        def resolvable(path: str) -> bool:
            node: Any = doc
            for key in path.split("."):
                if isinstance(node, dict) and key in node:
                    node = node[key]
                else:
                    # A null branch (market.closing before a rotation) is legitimate:
                    # the key exists, its subtree does not yet.
                    return node is None
            return True

        bad = sorted(p for p in _bound(_INDEX) if not resolvable(p))
        assert bad == [], bad


class TestEngineStatusPanel:
    def test_the_ten_named_engines_are_reported(self, run: ArcRuntime) -> None:
        rows = engine_status(run)
        assert [r["engine"] for r in rows][:10] == [
            "Market Engine", "Window Engine", "Decision Engine", "Risk Engine",
            "Limit Order Engine", "Recovery Engine", "Provider", "WebSocket", "RPC",
            "Wallet",
        ]

    def test_every_state_is_one_of_the_five(self, run: ArcRuntime) -> None:
        allowed = {"Running", "Waiting", "Reconnecting", "Warning", "Error"}
        assert {r["state"] for r in engine_status(run)} <= allowed

    def test_waiting_is_yellow_and_never_red(self, run: ArcRuntime) -> None:
        """An idle engine coloured red trains the operator to ignore red."""
        for row in engine_status(run):
            if row["state"] == "Waiting":
                assert row["light"] == "YELLOW"
            if row["state"] == "Running":
                assert row["light"] == "GREEN"

    def test_a_closed_system_gate_shows_the_reason_on_the_risk_row(
        self, run: ArcRuntime
    ) -> None:
        run.state.disable_trading("SPEC_UNVERIFIED")
        risk = next(r for r in engine_status(run) if r["engine"] == "Risk Engine")
        assert risk["state"] == "Error"
        assert "SPEC_UNVERIFIED" in risk["detail"]


class TestPreflight:
    def test_all_fifteen_checks_are_present(self, run: ArcRuntime) -> None:
        names = [c["check"] for c in preflight(run)["checks"]]
        assert names == [
            "Configuration", "SQLite", "Runtime", "Wallet", "Provider", "RTDS", "Clock",
            "Feed", "PTB", "Recovery", "Risk Engine", "Decision Engine",
            "Limit Order Engine", "WebSocket", "RPC",
        ]

    def test_every_result_is_pass_warning_or_fail(self, run: ArcRuntime) -> None:
        assert {c["result"] for c in preflight(run)["checks"]} <= {"PASS", "WARNING", "FAIL"}

    def test_a_warning_never_becomes_the_overall_fail(self, run: ArcRuntime) -> None:
        report = preflight(run)
        has_fail = any(c["result"] == "FAIL" for c in report["checks"])
        assert (report["result"] == "FAIL") is has_fail

    def test_every_non_pass_line_names_what_is_wrong(self, run: ArcRuntime) -> None:
        """FAIL with no detail sends the operator to the logs."""
        for check in preflight(run)["checks"]:
            if check["result"] != "PASS":
                assert check["detail"], check

    def test_an_idle_runtime_does_not_fail_the_check_that_gates_its_own_start(
        self, run: ArcRuntime
    ) -> None:
        """Preflight runs BEFORE V2 starts, against a runtime that has never been
        run. If "not running yet" or "no feed yet" counted as FAIL, the check that
        guards the start would make the start permanently impossible."""
        report = preflight(run)
        failed = {c["check"] for c in report["checks"] if c["result"] == "FAIL"}
        assert failed <= {"Risk Engine"}, failed

    def test_ready_is_derived_and_is_not_a_sixth_runtime_status(
        self, run: ArcRuntime
    ) -> None:
        """Runtime Verification is a preflight conclusion, not a RuntimeStatus. The
        status set is exactly the five the contract names; a sixth would be a second
        answer to "is it running" that could disagree with the first."""
        from arc.runtime.engine import RuntimeStatus

        report = preflight(run)
        # Idle: never ready, whatever the individual checks say.
        assert report["ready"] is False
        assert 'data-f="preflight.ready"' in _OPS
        assert not hasattr(RuntimeStatus, "READY")
        assert "READY" not in {
            v for k, v in vars(RuntimeStatus).items() if isinstance(v, str) and k.isupper()
        }

    def test_the_failing_names_the_start_route_reports_are_real_keys(
        self, run: ArcRuntime
    ) -> None:
        """`/start` builds its 409 detail out of these. A key that does not exist
        would raise inside the refusal and turn a clear "V2 preflight failed:
        Risk Engine" into a 500 with no reason at all."""
        for check in preflight(run)["checks"]:
            assert set(check) == {"check", "result", "detail"}


class TestDerivedStage:
    def test_no_market_waits_for_a_window(self, run: ArcRuntime) -> None:
        payload = derived_payload(run, None)
        assert payload["loe_stage"] == "WAITING_FOR_WINDOW"
        assert payload["loe_stages"] == list(LOE_STAGES)

    def test_the_stage_is_always_one_the_markup_can_highlight(self, run: ArcRuntime) -> None:
        run.rotator.advance(_NOW)
        assert derived_payload(run, run.rotator.current)["loe_stage"] in LOE_STAGES

    def test_frozen_values_are_strings_or_null_never_numbers(self, run: ArcRuntime) -> None:
        run.rotator.advance(_NOW)
        payload = derived_payload(run, run.rotator.current)
        for key in ("locked_trigger", "buffer", "frozen_twap"):
            assert payload[key] is None or isinstance(payload[key], str)
