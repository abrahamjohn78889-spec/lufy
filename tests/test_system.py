"""SYSTEM: every spec'd field shipped, bound, read-only, and never fabricated.

The gap this closes is the mirror of the one `test_ops_deck.py` pins. That test
catches a `data-f` the backend does not ship. This one catches the opposite — a
field the backend computes that no panel renders — which is worse, because it
looks like working code and the operator still has to SSH in to read the value.

Also pinned: the page is read-only, and an unavailable fact says so verbatim
rather than being guessed at.
"""

from __future__ import annotations

import io
import logging
import re
from pathlib import Path
from typing import Any

import pytest
from conftest import VALID_TRADING_VALUES

from arc.api.models import _UNAVAILABLE, _git_commit, _memory, system_payload
from arc.clock import FrozenClock
from arc.config import ArcSettings, Settings, build_trading_config
from arc.execution.v1_paper import PaperExecutor
from arc.market.feed import RtdsFeed
from arc.runtime.engine import ArcRuntime
from arc.runtime.state import RuntimeState
from arc.storage.store import Store

_INDEX = (Path(__file__).resolve().parent.parent / "arc" / "web" / "index.html").read_text(
    encoding="utf-8"
)
_SYS = _INDEX.split('id="ws-system"', 1)[1].split("</main>", 1)[0]
_NOW = 1_754_400_000.0

# The System page spec, field by field, mapped to the payload key that carries it.
# Written as the mapping so that deleting a key, or shipping one nothing renders,
# fails here rather than being noticed by an operator who went back to SSH.
_SPEC: dict[str, str] = {
    "VPS Hostname": "hostname",
    "Operating System": "operating_system",
    "CPU": "cpu",
    "Memory": "memory",
    "Disk": "disk_total_gb",
    "SQLite Status": "sqlite_status",
    "PM2 Status": "pm2_status",
    "Python Version": "python_version",
    "ARC Version": "arc_version",
    "Git Commit": "git_commit",
    "Active Provider": "active_provider",
    "Wallet": "wallet",
    "RTDS": "rtds",
    "WebSocket": "websocket",
    "RPC": "rpc",
    "Restart Count": "restart_count",
    "Runtime Uptime": "runtime_uptime_seconds",
}


def _build(store: Store, clock: FrozenClock) -> ArcRuntime:
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
        logger=logging.getLogger("arc.test.system"),
    )


@pytest.fixture
def run(tmp_path: Any) -> ArcRuntime:
    store = Store(f"{tmp_path}/arc.db")
    store.migrate(_NOW)
    return _build(store, FrozenClock(_NOW))


class TestEverySpecFieldIsShipped:
    def test_the_payload_carries_every_named_field(self, run: ArcRuntime) -> None:
        payload = system_payload(run, _NOW)
        missing = sorted(key for key in _SPEC.values() if key not in payload)
        assert missing == [], missing

    def test_every_named_field_is_rendered(self) -> None:
        """A computed field nothing binds is a value the operator still SSHes for."""
        bound = set(re.findall(r'data-f="system\.([^"]+)"', _SYS))
        # Telegram is the one row read from `settings`, which owns the credential.
        assert 'data-f="settings.telegram_configured"' in _SYS
        missing = sorted(f"{name} ({key})" for name, key in _SPEC.items() if key not in bound)
        assert missing == [], missing

    def test_no_shipped_field_is_silently_dropped(self, run: ArcRuntime) -> None:
        """The reverse of test_ops_deck: a key computed on every frame and never shown."""
        bound = set(re.findall(r'data-f="system\.([^"]+)"', _INDEX))
        unrendered = sorted(set(system_payload(run, _NOW)) - bound)
        assert unrendered == [], unrendered


class TestReadOnly:
    def test_nothing_on_the_page_is_editable(self) -> None:
        """"Everything read-only." An input here would imply the host is tunable."""
        assert "<input" not in _SYS
        assert "<select" not in _SYS
        assert "<textarea" not in _SYS

    def test_the_only_control_is_the_local_backup(self) -> None:
        """The System page is read-only except for the local backup and the Telegram
        send-test button (Phase 2 moved Telegram to System with an explicit TEST
        control). Nothing else may be tunable from this page."""
        assert set(re.findall(r"<button[^>]*id=\"([^\"]+)\"", _SYS)) == {
            "backup", "telegram-test"
        }


class TestRestartCount:
    def test_it_starts_at_zero_before_the_first_run(self, run: ArcRuntime) -> None:
        assert run.restart_count == 0

    def test_it_survives_a_restart(self, tmp_path: Any) -> None:
        """A counter reset on boot always reads 1 and hides a PM2 crash loop."""
        store = Store(f"{tmp_path}/arc.db")
        store.migrate(_NOW)
        store.set_runtime_state("restart_count", "13", _NOW)
        assert _build(store, FrozenClock(_NOW)).restart_count == 13


class TestNothingIsFabricated:
    def test_memory_is_a_real_reading_or_says_it_is_not(self) -> None:
        """Q3's no-fabrication rule, applied to a read-only host fact."""
        value = _memory()
        assert value == _UNAVAILABLE or re.fullmatch(r"[0-9]+\.[0-9] GB", value)

    def test_the_unavailable_string_names_why(self) -> None:
        assert "Official API not available" in _UNAVAILABLE

    def test_the_commit_is_read_from_git_not_guessed(self) -> None:
        commit = _git_commit()
        assert commit == _UNAVAILABLE or re.fullmatch(r"[0-9a-f]{12}", commit)

    def test_pm2_status_reports_the_absence_as_a_fact(self, run: ArcRuntime) -> None:
        """"Not managed" is true information. UNKNOWN would read as a broken probe."""
        status = system_payload(run, _NOW)["pm2_status"]
        assert status.startswith("managed (id ") or status == "not managed by PM2"


class TestUptime:
    def test_uptime_is_zero_before_the_runtime_starts(self, run: ArcRuntime) -> None:
        assert system_payload(run, _NOW)["runtime_uptime_seconds"] == 0.0

    def test_uptime_never_goes_negative(self, run: ArcRuntime) -> None:
        """A clock stepping backwards must not render as a negative uptime."""
        run.started_at = _NOW
        assert system_payload(run, _NOW - 5.0)["runtime_uptime_seconds"] == 0.0
