"""CLI: `arc doctor` validates and reports; `arc run` refuses to fake a runtime."""

from __future__ import annotations

import io
import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from conftest import VALID_TRADING_VALUES, WINDOW_TS

from arc.cli import doctor, main, run
from arc.clock import FrozenClock


@pytest.fixture(autouse=True)
def _isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Every doctor test gets its own db/log dir and no real .env leaking in."""
    monkeypatch.chdir(tmp_path)
    for key in list(os.environ):
        if key.startswith("ARC_"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("ARC_DB_PATH", str(tmp_path / "arc.db"))
    monkeypatch.setenv("ARC_LOG_DIR", str(tmp_path / "logs"))
    for key, value in VALID_TRADING_VALUES.items():
        monkeypatch.setenv(f"ARC_{key.upper()}", value)
    yield


class TestDoctorHappyPath:
    def test_valid_config_returns_zero(self) -> None:
        out = io.StringIO()
        assert doctor(out, FrozenClock(now=float(WINDOW_TS))) == 0

    def test_reports_phase_1_ok(self) -> None:
        out = io.StringIO()
        doctor(out, FrozenClock(now=float(WINDOW_TS)))
        assert "Phase 1 OK" in out.getvalue()

    def test_reports_every_configured_section(self) -> None:
        out = io.StringIO()
        doctor(out, FrozenClock(now=float(WINDOW_TS)))
        text = out.getvalue()
        for section in (
            "CONFIGURATION", "CREDENTIALS", "STORAGE", "EXECUTION WINDOWS",
            "TRADING PARAMETERS", "THRESHOLDS", "CURRENT MARKET", "WARNINGS",
        ):
            assert section in text

    def test_credentials_show_unset_never_the_value(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ARC_POLYMARKET_API_KEY", "not-a-real-secret-value")
        out = io.StringIO()
        doctor(out, FrozenClock(now=float(WINDOW_TS)))
        text = out.getvalue()
        assert "not-a-real-secret-value" not in text
        assert "SET" in text

    def test_first_run_seeds_settings_into_storage(self, tmp_path: Path) -> None:
        out = io.StringIO()
        doctor(out, FrozenClock(now=float(WINDOW_TS)))
        from arc.storage.store import Store

        store = Store(tmp_path / "arc.db")
        assert store.has_settings() is True
        store.close()

    def test_second_run_reports_sqlite_as_the_config_source(self, tmp_path: Path) -> None:
        out1 = io.StringIO()
        doctor(out1, FrozenClock(now=float(WINDOW_TS)))
        out2 = io.StringIO()
        doctor(out2, FrozenClock(now=float(WINDOW_TS)))
        assert "SQLite settings table" in out2.getvalue()


class TestDoctorFatalConfig:
    def test_invalid_mode_returns_one(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ARC_MODE", "TESTNET")
        out = io.StringIO()
        assert doctor(out, FrozenClock(now=float(WINDOW_TS))) == 1

    def test_invalid_mode_report_says_fatal(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ARC_MODE", "TESTNET")
        out = io.StringIO()
        doctor(out, FrozenClock(now=float(WINDOW_TS)))
        assert "FATAL" in out.getvalue()

    def test_non_loopback_bind_returns_one(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ARC_API_BIND", "0.0.0.0")
        out = io.StringIO()
        assert doctor(out, FrozenClock(now=float(WINDOW_TS))) == 1

    def test_a_broken_trading_invariant_returns_one(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ARC_EXECUTION_WINDOWS", "15,15,7,5,3")  # duplicate
        out = io.StringIO()
        assert doctor(out, FrozenClock(now=float(WINDOW_TS))) == 1

    def test_v2_without_credentials_returns_one(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ARC_MODE", "V2")
        out = io.StringIO()
        assert doctor(out, FrozenClock(now=float(WINDOW_TS))) == 1

    def test_a_fatal_config_never_creates_a_database_file(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A bad config fails before a database is created for a config that
        will never run — config is validated first, storage second."""
        monkeypatch.setenv("ARC_MODE", "TESTNET")
        out = io.StringIO()
        doctor(out, FrozenClock(now=float(WINDOW_TS)))
        assert not (tmp_path / "arc.db").exists()


class TestDoctorAdvisoryWarningsDoNotBlock:
    def test_opposing_directions_allowed_is_a_warning_not_fatal(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ARC_ALLOW_OPPOSING_DIRECTIONS", "true")
        out = io.StringIO()
        assert doctor(out, FrozenClock(now=float(WINDOW_TS))) == 0
        assert "⚠" in out.getvalue()

    def test_no_warnings_reports_none(self) -> None:
        out = io.StringIO()
        doctor(out, FrozenClock(now=float(WINDOW_TS)))
        assert "none" in out.getvalue()


class TestRunAlwaysRefuses:
    def test_run_returns_one(self) -> None:
        assert run(io.StringIO()) == 1

    def test_run_explains_why_rather_than_pretending_to_work(self) -> None:
        out = io.StringIO()
        run(out)
        text = out.getvalue()
        assert "not available" in text
        assert "arc doctor" in text

    def test_run_never_returns_zero(self) -> None:
        """A6 Rule 2: a fake runtime that looked healthy is worse than an error."""
        assert run(io.StringIO()) != 0


class TestMainDispatchesSubcommands:
    def test_doctor_subcommand_runs_doctor(self, capsys: pytest.CaptureFixture[str]) -> None:
        code = main(["doctor"])
        assert code in (0, 1)
        assert "ARC doctor" in capsys.readouterr().out

    def test_run_subcommand_runs_run(self, capsys: pytest.CaptureFixture[str]) -> None:
        code = main(["run"])
        assert code == 1
        assert "not available" in capsys.readouterr().out

    def test_no_subcommand_is_a_usage_error(self) -> None:
        with pytest.raises(SystemExit):
            main([])

    def test_unknown_subcommand_is_a_usage_error(self) -> None:
        with pytest.raises(SystemExit):
            main(["bogus"])
