"""CONFIGURATION SYNCHRONIZATION: no hidden configuration, and a stated precedence.

Three separate promises, each with a failure the operator would never see as an
error:

  .env.example  — a setting that exists in code but not in this file is
                  configuration reachable only by reading source. The operator
                  would run the built-in default believing it was the only value.
  precedence    — CLI over SQLite over .env over defaults. Get the middle rung
                  wrong and a buffer the operator changed in the UI is silently
                  reverted by a stale .env on the next restart.
  provider      — one provider supplies all TWAP data or none of it. A silent
                  fallback means trading against a different price source than
                  the one the dashboard names.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from conftest import VALID_TRADING_VALUES

from arc.config import (
    LOG_LEVELS,
    THEMES,
    TRADING_KEYS,
    ArcSettings,
    build_trading_config,
    load_settings,
)
from arc.domain.enums import Mode
from arc.errors import ConfigInvariantError
from arc.market.feed import RTDS_URL, BackoffPolicy, RtdsFeed
from arc.market.providers import ProviderName, build_provider

_ROOT = Path(__file__).resolve().parent.parent
_ENV_EXAMPLE = (_ROOT / ".env.example").read_text(encoding="utf-8")

# `ARC_FOO=` on its own line, or commented out as `# ARC_FOO=` where the shipped
# default is already correct. Both count as documented; a key absent in BOTH
# forms is the failure this file exists to catch.
_KEY = re.compile(r"^#?\s*(ARC_[A-Z0-9_]+)=", re.MULTILINE)

_DOCUMENTED: frozenset[str] = frozenset(_KEY.findall(_ENV_EXAMPLE))


def _env_name(field: str) -> str:
    return f"ARC_{field.upper()}"


class TestEnvExampleIsComplete:
    def test_every_settings_field_appears(self) -> None:
        """Nothing configurable may remain hidden.

        Driven off model_fields rather than a hand-written list, so a field added
        in a later change fails here instead of being discovered by an operator
        who could not find the knob.
        """
        missing = sorted(
            _env_name(name)
            for name in ArcSettings.model_fields
            if _env_name(name) not in _DOCUMENTED
        )
        assert missing == [], missing

    def test_every_documented_key_is_read_by_something(self) -> None:
        """The reverse error: a key in the file that no field reads is a setting
        the operator sets, restarts for, and sees no effect from."""
        known = {_env_name(name) for name in ArcSettings.model_fields}
        # The one deliberate alias. Both spellings are accepted by the field.
        known.add("ARC_CHAINLINK_FEBS_ID")
        unread = sorted(k for k in _DOCUMENTED if k not in known)
        assert unread == [], unread

    def test_every_trading_key_is_documented(self) -> None:
        for key in TRADING_KEYS:
            assert _env_name(key) in _DOCUMENTED, key

    def test_no_credential_ships_with_a_value(self) -> None:
        """An example file with a real-looking key gets copied to .env verbatim."""
        for line in _ENV_EXAMPLE.splitlines():
            if line.startswith(("ARC_POLYMARKET_", "ARC_CHAINLINK_", "ARC_TELEGRAM_BOT")):
                assert line.endswith("="), line


class TestPrecedence:
    def test_stored_settings_beat_env(self) -> None:
        """Rung 2 over rung 3. The operator edited a buffer in the UI; a stale
        .env must not revert it on the next restart."""
        env = ArcSettings(**{k: v for k, v in VALID_TRADING_VALUES.items()})
        stored = dict(VALID_TRADING_VALUES) | {"position_notional_usd": "99.00"}
        settings = load_settings(env, stored)
        assert str(settings.trading.position_notional_usd) == "99.00"
        assert settings.seeded_from_env is False

    def test_env_seeds_when_storage_is_empty(self) -> None:
        """Rung 3 over rung 4, first run only."""
        env = ArcSettings(**{k: v for k, v in VALID_TRADING_VALUES.items()})
        settings = load_settings(env, {})
        assert str(settings.trading.position_notional_usd) == "25.00"
        assert settings.seeded_from_env is True

    def test_cli_mode_beats_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Rung 1. `arc run --mode=v2` is how cli.run overrides the file, and the
        override is a plain assignment on the settings object it then loads."""
        monkeypatch.setenv("ARC_MODE", "V1")
        env = ArcSettings(**{k: v for k, v in VALID_TRADING_VALUES.items()})
        assert env.mode is Mode.V1
        env.mode = Mode.V2
        assert env.mode is Mode.V2

    def test_no_trading_value_has_a_built_in_default(self) -> None:
        """Rung 4 is infrastructure only. A substituted buffer is indistinguishable,
        afterwards, from one the operator chose."""
        with pytest.raises(ConfigInvariantError):
            build_trading_config({})

    def test_a_restart_restores_the_stored_configuration(self, tmp_path: Path) -> None:
        """The whole point of rung 2: PM2 restart, VPS reboot, process restart —
        the operator's last saved configuration comes back without .env."""
        from arc.clock import FrozenClock
        from arc.storage.store import Store

        clock = FrozenClock(1_754_400_000.0)
        path = f"{tmp_path}/arc.db"

        store = Store(path)
        store.migrate(clock.now())
        env = ArcSettings(**{k: v for k, v in VALID_TRADING_VALUES.items()})
        first = load_settings(env, store.load_settings())
        assert first.seeded_from_env is True
        store.save_settings(first.trading.as_storage_dict(), clock.now())
        # The operator edits one value in the UI and it is written through.
        store.save_settings({"position_notional_usd": "77.00"}, clock.now())
        store.close()

        # The process dies. A .env that still says 25.00 is read on the way back up.
        reopened = Store(path)
        second = load_settings(env, reopened.load_settings())
        reopened.close()
        assert str(second.trading.position_notional_usd) == "77.00"
        assert second.seeded_from_env is False


class TestValidatedInfrastructure:
    @pytest.mark.parametrize("value", ["TRACE", "info ", "verbose", ""])
    def test_an_unknown_log_level_is_refused(self, value: str) -> None:
        """A silent fall back to INFO makes DEBUG look like it produced no logging."""
        if value.strip().upper() in LOG_LEVELS:
            pytest.skip("valid after normalisation")
        with pytest.raises(ValueError):
            ArcSettings(log_level=value)

    def test_a_known_log_level_normalises(self) -> None:
        assert ArcSettings(log_level="debug").log_level == "DEBUG"

    @pytest.mark.parametrize("value", ["solarized", "DARKK", ""])
    def test_an_unknown_theme_is_refused(self, value: str) -> None:
        with pytest.raises(ValueError):
            ArcSettings(theme=value)

    def test_both_shipped_themes_are_accepted(self) -> None:
        for theme in THEMES:
            assert ArcSettings(theme=theme).theme == theme

    def test_an_unknown_timezone_is_refused(self) -> None:
        """Raised here, not inside the log formatter, where logging's own error
        handling would swallow it and the timestamps would quietly stay wrong."""
        with pytest.raises(ValueError):
            ArcSettings(timezone="Mars/Olympus")

    def test_a_known_timezone_is_accepted_and_blank_means_host(self) -> None:
        assert ArcSettings(timezone="Europe/London").timezone == "Europe/London"
        assert ArcSettings().timezone == ""

    @pytest.mark.parametrize(
        "field", ["api_port", "reconnect_backoff_ms", "reconnect_backoff_max_ms", "refresh_rate_ms"]
    )
    def test_a_non_positive_interval_is_refused(self, field: str) -> None:
        """0 ms is not a slower setting, it is a busy loop."""
        with pytest.raises(ValueError):
            ArcSettings(**{field: 0})

    def test_a_backoff_ceiling_below_the_initial_delay_is_refused(self) -> None:
        """A shrinking ladder retries fastest exactly when the venue is least able
        to answer."""
        with pytest.raises(ValueError):
            ArcSettings(reconnect_backoff_ms=5000, reconnect_backoff_max_ms=1000)

    def test_endpoint_defaults_come_from_the_official_sdk(self) -> None:
        """Not hand-copied literals: a copied hostname stops matching the SDK the
        day Polymarket moves one, and ARC dials an address nobody serves."""
        import polymarket

        env = ArcSettings()
        assert env.rtds_url == polymarket.PRODUCTION.rtds_ws_url
        assert env.clob_host == polymarket.PRODUCTION.clob_url
        assert env.clob_http_url == polymarket.PRODUCTION.clob_url
        assert env.clob_ws_url == polymarket.PRODUCTION.clob_user_ws_url
        assert env.chain_id == polymarket.PRODUCTION.chain_id
        assert env.network_id == polymarket.PRODUCTION.name


class TestProviderSelection:
    def test_rtds_is_the_default(self) -> None:
        assert ArcSettings().twap_provider == ProviderName.RTDS.value

    def test_an_unknown_provider_is_refused_at_settings_load(self) -> None:
        """Refused, never silently left on RTDS: the operator would believe the
        switch took and be trading a different price source than the UI names."""
        with pytest.raises(ValueError):
            ArcSettings(twap_provider="COINBASE")

    def test_chainlink_is_nameable_and_refused_at_build(self) -> None:
        """A named-but-unimplemented provider must read as "not built yet", not as
        a typo. Selecting it is fatal until official documentation is verified —
        a stub against a guessed feed ID would produce prices that look real."""
        assert ArcSettings(twap_provider="CHAINLINK").twap_provider == "CHAINLINK"
        with pytest.raises(ConfigInvariantError) as exc:
            build_provider("CHAINLINK", _clock())
        assert "not implemented" in str(exc.value)

    def test_no_mixed_provider_operation(self) -> None:
        """build_provider returns exactly one feed. There is no path that returns
        two, and none that falls back to a second while the first is selected."""
        provider = build_provider("RTDS", _clock())
        assert isinstance(provider, RtdsFeed)

    def test_the_url_and_backoff_are_configured_not_hardcoded(self) -> None:
        """A relay address only reachable by editing source is an address the
        operator cannot move when Polymarket moves it."""
        policy = BackoffPolicy(initial_seconds=1.5, max_seconds=12.0)
        provider = build_provider(
            "RTDS", _clock(), url="wss://relay.example/ws", backoff=policy
        )
        assert provider.url == "wss://relay.example/ws"
        assert build_provider("RTDS", _clock()).url == RTDS_URL


def _clock() -> object:
    from arc.clock import FrozenClock

    return FrozenClock(1_754_400_000.0)
