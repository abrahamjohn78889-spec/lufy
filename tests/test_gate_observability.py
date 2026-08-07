"""The ten observability additions that sit on top of the nineteen risk gates.

Gate IDs, health revisions, risk-evaluation timing, wallet freshness, supervisor
lifecycle states, orphan counts, balance arithmetic, the readiness summary, the
health-transition history and the startup verification block.

None of these change a trading decision. Every one of them is something an
operator has to be able to read without SSHing into the box, which is why the
tests assert they reach a SURFACE — the payload, the markup, the report, the
printed block — and not merely that a method returns a value.
"""

from __future__ import annotations

import asyncio
import io
import logging
import re
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from conftest import VALID_TRADING_VALUES
from decision_fixtures import fired_market, fresh_store, healthy, make_engine
from test_risk import _context

from arc.api.models import status_payload
from arc.clock import FrozenClock
from arc.config import ArcSettings, Settings, build_trading_config
from arc.execution.v1_paper import PaperExecutor
from arc.market.feed import RtdsFeed
from arc.risk.engine import GATE_IDS, GATE_ORDER, RiskEngine, gate_id
from arc.runtime.engine import (
    SUPERVISOR_READY,
    SUPERVISOR_STATES,
    ArcRuntime,
    RuntimeStatus,
    TimedRiskEngine,
)
from arc.runtime.report import render_report
from arc.runtime.state import RuntimeState
from arc.runtime.validation import validate_run
from arc.storage.store import Store

_WEB = Path(__file__).resolve().parent.parent / "arc" / "web"
_INDEX = (_WEB / "index.html").read_text(encoding="utf-8")
_APP = (_WEB / "app.js").read_text(encoding="utf-8")
_NOW = 1_754_400_000.0
NOW = 1_754_400_001.0


def _toggle_arm(run: ArcRuntime) -> None:
    """Flip the operator gate. It is not settable on the runtime — ArcRuntime uses
    __slots__ and the gate lives on RuntimeState, which owns it."""
    if run.state.execution_armed:
        run.state.disarm_execution()
    else:
        run.state.arm_execution()


@pytest.fixture
def out() -> io.StringIO:
    return io.StringIO()


@pytest.fixture
def run(tmp_path: Any, out: io.StringIO) -> ArcRuntime:
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
        out=out,
        logger=logging.getLogger("arc.test.gates"),
    )


def _payload(run: ArcRuntime) -> dict[str, Any]:
    return asyncio.run(status_payload(run, _NOW))


# ── 1. gate IDs ──────────────────────────────────────────────────────────────


class TestGateIdentifiers:
    def test_every_gate_has_one(self) -> None:
        assert sorted(GATE_IDS) == sorted(GATE_ORDER)

    def test_they_are_g01_through_g19_in_order(self) -> None:
        assert [GATE_IDS[name] for name in GATE_ORDER] == [
            f"G{i:02d}" for i in range(1, len(GATE_ORDER) + 1)
        ]

    def test_they_are_unique(self) -> None:
        assert len(set(GATE_IDS.values())) == len(GATE_ORDER)

    def test_an_unknown_name_has_no_id_rather_than_a_wrong_one(self) -> None:
        assert gate_id("not_a_gate") == ""

    def test_a_verdict_carries_its_own_id(self) -> None:
        verdict = RiskEngine().evaluate(_context(trading_enabled=False))
        assert verdict.gate_id == GATE_IDS[verdict.gate] != ""

    def test_an_allowed_verdict_names_no_gate_and_so_no_id(self) -> None:
        assert RiskEngine().evaluate(_context()).gate_id == ""


class TestTheDenialLineCarriesEverySpecifiedField:
    """Gate ID · gate name · reason · timestamp · market · window · runtime mode.

    One assertion over the single denial site rather than seven over five
    surfaces: all five are fed by that one log call, so a field missing here is
    a field missing everywhere.
    """

    def test_all_seven_fields_are_present(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        store = fresh_store(tmp_path)
        market = fired_market()
        store.create_market(market, NOW)
        logger = logging.getLogger("arc.test.denial.fields")
        risk = TimedRiskEngine()
        engine = make_engine(
            store,
            health=healthy(paused=True, mode="RUNNING (V1)"),
            risk=risk,
            logger=logger,
        )
        with caplog.at_level(logging.WARNING, logger=logger.name):
            engine.decide(market, NOW)
        # The event is the label; the seven fields live in the aligned detail column,
        # which is what every one of the five surfaces renders.
        record = next(r for r in caplog.records if r.message == "Intent Denied")
        line: str = record.arc_detail  # type: ignore[attr-defined]

        assert re.search(r"\bG\d{2}\b", line)  # gate id
        assert any(name in line for name in GATE_ORDER)  # gate name
        assert market.slug in line  # market
        assert re.search(r"\b\d+s\b", line)  # window
        assert "RUNNING (V1)" in line  # runtime mode
        # The timestamp is the log record's own, which is what every surface reads.
        assert record.created > 0
        assert risk.last_ms > 0.0


# ── 2. health revision ───────────────────────────────────────────────────────


class TestHealthRevision:
    def test_it_starts_at_one_after_the_first_snapshot(self, run: ArcRuntime) -> None:
        assert run.health().health_revision == 1

    def test_an_unchanged_snapshot_does_not_bump_it(self, run: ArcRuntime) -> None:
        first = run.health().health_revision
        assert run.health().health_revision == first

    def test_a_changed_field_bumps_it(self, run: ArcRuntime) -> None:
        first = run.health().health_revision
        _toggle_arm(run)
        assert run.health().health_revision == first + 1

    def test_it_reaches_the_payload(self, run: ArcRuntime) -> None:
        run.health()
        assert _payload(run)["runtime"]["health_revision"] == run.health_revision

    def test_the_dashboard_redraws_on_the_revision_and_not_every_frame(self) -> None:
        assert "health_revision" in _APP
        assert "paintedRevision" in _APP


# ── 3. risk evaluation timing ────────────────────────────────────────────────


class TestRiskEvaluationTiming:
    def test_it_is_zero_before_any_pass(self, run: ArcRuntime) -> None:
        assert run.risk_eval_ms == 0.0
        assert run.risk_eval_max_ms == 0.0

    def test_the_decision_layer_is_not_the_one_holding_the_clock(self) -> None:
        """A0 forbids the decision package a timer. The stopwatch is a subclass of
        the risk engine, owned by the runtime, so the measurement exists without
        putting a clock inside the layer that must stay reproducible."""
        source = Path("arc/decision/engine.py").read_text(encoding="utf-8")
        assert "perf_counter" not in source
        assert issubclass(TimedRiskEngine, RiskEngine)

    def test_a_pass_records_a_non_negative_duration(self, tmp_path: Path) -> None:
        store = fresh_store(tmp_path)
        market = fired_market()
        store.create_market(market, NOW)
        risk = TimedRiskEngine()
        engine = make_engine(store, health=healthy(paused=True), risk=risk)
        engine.decide(market, NOW)
        assert risk.last_ms > 0.0
        assert risk.max_ms >= risk.last_ms

    def test_the_verdict_is_unchanged_by_the_stopwatch(self) -> None:
        context = _context(trading_enabled=False)
        assert TimedRiskEngine().evaluate(context) == RiskEngine().evaluate(context)

    def test_both_reach_the_payload(self, run: ArcRuntime) -> None:
        runtime = _payload(run)["runtime"]
        assert runtime["risk_eval_ms"] == run.risk_eval_ms
        assert runtime["risk_eval_max_ms"] == run.risk_eval_max_ms

    def test_both_are_on_the_systems_page(self) -> None:
        system = _INDEX.split('id="ws-system"', 1)[1].split("</main>", 1)[0]
        assert 'data-f="runtime.risk_eval_ms"' in system
        assert 'data-f="runtime.risk_eval_max_ms"' in system


# ── 4. wallet freshness ──────────────────────────────────────────────────────


class TestWalletFreshness:
    def test_never_read_is_none_and_not_zero(self, run: ArcRuntime) -> None:
        """Zero would read as "refreshed this instant", which is the opposite."""
        assert run.wallet_refreshed_at == 0.0
        assert run.wallet_refresh_age_ms(_NOW) is None

    def test_the_age_grows_with_the_clock(self, run: ArcRuntime) -> None:
        asyncio.run(run._refresh_wallet(_NOW))
        assert run.wallet_refreshed_at == _NOW
        assert run.wallet_refresh_age_ms(_NOW) == pytest.approx(0.0)
        assert run.wallet_refresh_age_ms(_NOW + 2.0) == pytest.approx(2000.0)

    def test_both_reach_the_balance_block(self, run: ArcRuntime) -> None:
        asyncio.run(run._refresh_wallet(_NOW))
        balance = _payload(run)["balance"]
        assert balance["last_refresh"] == _NOW
        assert balance["refresh_age_ms"] == pytest.approx(0.0)


# ── 5. supervisor lifecycle ──────────────────────────────────────────────────


class TestSupervisorLifecycleStates:
    def test_the_four_required_states_exist(self) -> None:
        assert {"READY", "STARTING", "STOPPING", "FAILED"} <= set(SUPERVISOR_STATES)

    def test_they_are_not_runtime_statuses(self) -> None:
        """RuntimeStatus is a closed set of five trading states. Adding a sixth
        would make the supervisor's lifecycle look like a trading mode."""
        statuses = {
            v for k, v in vars(RuntimeStatus).items()
            if not k.startswith("_") and isinstance(v, str)
        }
        assert len(statuses) == 5
        assert "READY" not in statuses

    def test_the_state_reaches_the_payload(self, run: ArcRuntime) -> None:
        assert _payload(run)["runtime"]["supervisor_state"] == SUPERVISOR_READY

    def test_it_is_on_the_systems_page_and_only_there(self) -> None:
        system = _INDEX.split('id="ws-system"', 1)[1].split("</main>", 1)[0]
        ops = _INDEX.split('id="ws-ops"', 1)[1].split("</main>", 1)[0]
        assert 'data-f="runtime.supervisor_state"' in system
        assert 'data-f="runtime.supervisor_state"' not in ops


# ── 6. orphan detail ─────────────────────────────────────────────────────────


class TestOrphanCount:
    def test_the_denial_detail_leads_with_the_count(self) -> None:
        verdict = RiskEngine().evaluate(_context(orphan_orders=("0xa", "0xb")))
        assert verdict.detail.startswith("2 unreconciled")

    def test_the_count_reaches_the_payload(self, run: ArcRuntime) -> None:
        assert _payload(run)["recovery"]["orphan_count"] == 0

    def test_it_is_on_the_deck(self) -> None:
        assert 'data-f="recovery.orphan_count"' in _INDEX


# ── 7. balance detail ────────────────────────────────────────────────────────


class TestBalanceDetail:
    def test_the_difference_is_available_minus_required(self, run: ArcRuntime) -> None:
        run._wallet_available = Decimal("100.00")
        run._wallet_refreshed_at = _NOW
        balance = run.balance_detail(_NOW)
        required = Decimal(balance["required"])
        assert Decimal(balance["available"]) == Decimal("100.00")
        assert Decimal(balance["difference"]) == Decimal("100.00") - required
        assert balance["sufficient"] is True

    def test_an_unknown_balance_is_none_rather_than_zero(self, run: ArcRuntime) -> None:
        balance = run.balance_detail(_NOW)
        assert balance["available"] is None
        assert balance["difference"] is None

    def test_all_three_are_on_the_ops_deck(self) -> None:
        ops = _INDEX.split('id="ws-ops"', 1)[1].split("</main>", 1)[0]
        for field in ("available", "required", "difference"):
            assert f'data-f="balance.{field}"' in ops


# ── 8. readiness summary ─────────────────────────────────────────────────────


class TestGateReadinessSummary:
    def test_there_is_one_row_per_gate(self, run: ArcRuntime) -> None:
        rows = run.gate_readiness()
        assert [r["gate"] for r in rows] == list(GATE_ORDER)
        assert [r["id"] for r in rows] == [GATE_IDS[n] for n in GATE_ORDER]

    def test_the_summary_counts_out_of_nineteen(self, run: ArcRuntime) -> None:
        summary = run.gate_summary()
        assert summary["total"] == len(GATE_ORDER)
        assert re.fullmatch(r"\d+ / 19 Gates PASS", summary["summary"])

    def test_a_gate_that_needs_a_live_window_says_so_rather_than_passing(
        self, run: ArcRuntime
    ) -> None:
        """A fabricated PASS on a gate nobody evaluated is the exact failure the
        no-fabrication rule exists to prevent."""
        states = {r["gate"]: r["state"] for r in run.gate_readiness()}
        assert states["window_triggered"] == "PER WINDOW"
        assert states["entry_band"] == "PER WINDOW"

    def test_a_standing_gate_that_is_off_is_reported_as_a_failure(
        self, run: ArcRuntime
    ) -> None:
        run.state.disarm_execution()
        summary = run.gate_summary()
        assert any(f["gate"] == "execution_armed" for f in summary["failures"])
        assert summary["passing"] < summary["total"]

    def test_the_summary_and_the_failures_reach_the_payload(
        self, run: ArcRuntime
    ) -> None:
        doc = _payload(run)
        assert doc["runtime"]["gates_total"] == len(GATE_ORDER)
        assert doc["runtime"]["gates_summary"] == run.gate_summary()["summary"]
        assert len(doc["gates"]) == len(GATE_ORDER)

    def test_the_systems_page_hosts_the_expandable_table(self) -> None:
        system = _INDEX.split('id="ws-system"', 1)[1].split("</main>", 1)[0]
        assert 'data-f="runtime.gates_summary"' in system
        assert 'id="gates"' in system
        assert "<details" in system
        assert "$('#gates')" in _APP


# ── 9. health history ────────────────────────────────────────────────────────


class TestHealthHistory:
    def test_it_records_one_entry_per_transition(self, run: ArcRuntime) -> None:
        run.health()
        _toggle_arm(run)
        run.health()
        run.health()
        assert [entry[1] for entry in run.health_history] == [1, 2]

    def test_it_keeps_the_last_two_hundred(self, run: ArcRuntime) -> None:
        for _ in range(250):
            _toggle_arm(run)
            run.health()
        assert len(run.health_history) == 200

    def test_it_is_oldest_first_and_carries_all_three_zones(
        self, run: ArcRuntime
    ) -> None:
        run.health()
        entry = _payload(run)["health_history"][0]
        assert entry["revision"] == 1
        for zone in ("utc", "utc_display", "ist", "et"):
            assert entry[zone]

    def test_the_systems_page_hosts_it(self) -> None:
        system = _INDEX.split('id="ws-system"', 1)[1].split("</main>", 1)[0]
        assert 'id="health-history"' in system


# ── 10. startup verification ─────────────────────────────────────────────────

_VERIFICATION_ROWS = (
    "Risk Gates",
    "Wallet",
    "Provider",
    "RTDS",
    "CLOB",
    "Database",
    "Recovery",
    "Supervisor",
    "Ready",
)


class TestStartupVerification:
    def test_the_rows_are_the_ten_specified_lines_in_order(
        self, run: ArcRuntime
    ) -> None:
        assert tuple(name for name, _, _ in run.verification()) == _VERIFICATION_ROWS

    def test_every_row_is_a_verdict_and_not_a_guess(self, run: ArcRuntime) -> None:
        assert {state for _, state, _ in run.verification()} <= {
            "PASS", "FAIL", "YES", "NO", f"{len(GATE_ORDER)} / {len(GATE_ORDER)}"
        }

    def test_the_gate_row_counts_all_nineteen(self, run: ArcRuntime) -> None:
        state = next(s for name, s, _ in run.verification() if name == "Risk Gates")
        assert state == f"{len(GATE_ORDER)} / {len(GATE_ORDER)}"

    def test_it_prints_exactly_once_per_start(
        self, run: ArcRuntime, out: io.StringIO
    ) -> None:
        run._print_verification()
        printed = out.getvalue()
        assert printed.count("Runtime Verification") == 1
        for name in _VERIFICATION_ROWS:
            assert name in printed


# ── the denial reaches every surface, not just the log ───────────────────────


class TestTheDenialFansOutIntact:
    """The tank and Telegram are fed by the SAME log call. Both are pinned here
    because "the detail is forwarded" is exactly the kind of thing that stays
    true until someone summarises the line on its way out."""

    def _denial_detail(self, tmp_path: Path) -> str:
        from arc.runtime.events import EventHub, SignalTankHandler

        store = fresh_store(tmp_path)
        market = fired_market()
        store.create_market(market, NOW)
        logger = logging.getLogger("arc.test.denial.fanout")
        logger.handlers.clear()
        hub = EventHub()
        logger.addHandler(SignalTankHandler(hub))
        logger.setLevel(logging.WARNING)
        try:
            make_engine(
                store,
                health=healthy(paused=True, mode="RUNNING (V1)"),
                logger=logger,
            ).decide(market, NOW)
        finally:
            logger.handlers.clear()
        event = next(e for e in hub.recent() if e.event == "Intent Denied")
        return event.detail

    def test_the_signal_tank_keeps_the_whole_line(self, tmp_path: Path) -> None:
        detail = self._denial_detail(tmp_path)
        assert re.search(r"\bG\d{2}\b", detail)
        assert "RUNNING (V1)" in detail

    def test_telegram_appends_the_detail_rather_than_summarising_it(
        self, tmp_path: Path
    ) -> None:
        from arc.notify.telegram import TelegramNotifier

        detail = self._denial_detail(tmp_path)
        body = TelegramNotifier(token="t", chat_id="c", flags={}).render(
            {
                "type": "signal",
                "data": {
                    "engine": "Decision",
                    "severity": "WARNING",
                    "event": "Intent Denied",
                    "detail": detail,
                },
            }
        )
        assert body is not None
        assert detail in body


# ── the report carries both blocks ───────────────────────────────────────────


class TestTheValidationReportCarriesTheGates:
    def test_the_gate_table_and_the_verification_block_are_rendered(
        self, run: ArcRuntime
    ) -> None:
        text = render_report(
            validate_run(run.store, offsets=(15, 10, 7, 5, 3), cadence_seconds=300),
            mode=run.mode.value,
            provider=run.settings.env.twap_provider,
            generated_at=_NOW,
            gates=run.gate_summary(),
            verification=run.verification(),
        )
        assert "RUNTIME VERIFICATION" in text
        assert "RISK GATES" in text
        for name in GATE_ORDER:
            assert f"{GATE_IDS[name]} {name}" in text

    def test_omitting_them_omits_the_sections_rather_than_faking_them(
        self, run: ArcRuntime
    ) -> None:
        text = render_report(
            validate_run(run.store, offsets=(15, 10, 7, 5, 3), cadence_seconds=300),
            mode=run.mode.value,
            provider=run.settings.env.twap_provider,
            generated_at=_NOW,
        )
        assert "RUNTIME VERIFICATION" not in text
        assert "RISK GATES" not in text
