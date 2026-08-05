"""TWAP provider selection: RTDS ships, Chainlink refuses, nothing else knows.

The contract this file enforces is negative as much as positive. RTDS must work with
no credentials and no extra configuration; Chainlink must be nameable and must fail
loudly rather than quietly falling back; and no strategy module may import a provider,
because the whole point of the interface is that changing the source of the price data
cannot change what the strategy does.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from arc.clock import FrozenClock
from arc.config import ArcSettings, load_settings
from arc.errors import ConfigInvariantError
from arc.market.feed import RtdsFeed
from arc.market.providers import ProviderName, TwapProvider, build_provider

# Every module that must remain ignorant of the provider. Stated as paths rather than
# as a package, because the guarantee is about specific engines, not about a directory
# that a later refactor might rename.
PROVIDER_BLIND = (
    "arc/market/ptb.py",
    "arc/windows",
    "arc/decision",
    "arc/risk",
    "arc/strategy",
)


def _clock() -> FrozenClock:
    return FrozenClock(now=1754400000.0)


class TestRtdsIsTheDefault:
    def test_the_default_provider_is_rtds(self) -> None:
        assert ArcSettings(_env_file=None).twap_provider == "RTDS"

    def test_it_builds_with_no_credentials(self) -> None:
        """V1 out of the box: no key, no secret, no feed ID, no extra configuration."""
        env = ArcSettings(_env_file=None)
        assert env.chainlink_api_key.get_secret_value() == ""
        assert env.chainlink_api_secret.get_secret_value() == ""
        assert env.chainlink_feed_id == ""
        provider = build_provider(env.twap_provider, _clock())
        assert isinstance(provider, RtdsFeed)

    def test_the_built_provider_satisfies_the_interface(self) -> None:
        assert isinstance(build_provider("RTDS", _clock()), TwapProvider)

    @pytest.mark.parametrize("name", ["rtds", " RTDS ", "Rtds"])
    def test_the_name_is_case_and_space_insensitive(self, name: str) -> None:
        """An operator typing lowercase must not get a fatal startup error."""
        assert isinstance(build_provider(name, _clock()), RtdsFeed)


class TestChainlinkIsConfigurationOnly:
    def test_it_is_a_nameable_provider(self) -> None:
        """So the refusal says "not implemented", not "unknown provider"."""
        assert ProviderName.CHAINLINK.value == "CHAINLINK"

    def test_selecting_it_is_a_fatal_configuration_error(self) -> None:
        with pytest.raises(ConfigInvariantError, match="not implemented"):
            build_provider("CHAINLINK", _clock())

    def test_it_does_not_silently_fall_back_to_rtds(self) -> None:
        """The failure this prevents: trading a different price source than configured."""
        with pytest.raises(ConfigInvariantError):
            build_provider("CHAINLINK", _clock())

    def test_no_chainlink_implementation_module_exists(self, source_root: Path) -> None:
        """A1 Rule 2: no stubs. A guessed feed ID yields prices that look real."""
        assert not list(source_root.glob("arc/**/chainlink*.py"))

    def test_no_feed_id_is_defaulted(self) -> None:
        assert ArcSettings(_env_file=None).chainlink_feed_id == ""

    def test_the_env_example_prepares_the_keys_and_leaves_them_blank(
        self, source_root: Path
    ) -> None:
        text = (source_root / ".env.example").read_text(encoding="utf-8")
        assert "ARC_TWAP_PROVIDER=RTDS" in text
        for key in ("ARC_CHAINLINK_API_KEY", "ARC_CHAINLINK_API_SECRET",
                    "ARC_CHAINLINK_FEED_ID"):
            assert f"{key}=\n" in text or text.rstrip().endswith(f"{key}=")


class TestAnUnknownProviderIsRefused:
    @pytest.mark.parametrize("name", ["", "PYTH", "rtds2"])
    def test_it_raises_rather_than_defaulting(self, name: str) -> None:
        with pytest.raises(ConfigInvariantError, match="TWAP_PROVIDER"):
            build_provider(name, _clock())


class TestChainlinkSecretsAreRedacted:
    def test_they_report_set_or_unset_never_a_value(self) -> None:
        env = ArcSettings(
            _env_file=None,
            chainlink_api_key="ck-secret-value",
            chainlink_api_secret="cs-secret-value",
        )
        dump = env.redacted_dump()
        assert dump["chainlink_api_key"] == "SET"
        assert dump["chainlink_api_secret"] == "SET"
        assert "ck-secret-value" not in str(dump)

    def test_they_reach_the_log_redaction_filter(self) -> None:
        env = ArcSettings(_env_file=None, chainlink_api_key="ck-secret-value")
        assert "ck-secret-value" in env.secret_values()


class TestV2DoesNotDependOnProviderCredentials:
    """MODE=V2 means "live at the venue". A blank Chainlink key is not a venue key."""

    def test_v2_boots_with_venue_keys_and_no_chainlink_keys(
        self, trading_values: dict[str, str]
    ) -> None:
        env = ArcSettings(
            _env_file=None,
            mode="V2",
            polymarket_api_key="k",
            polymarket_api_secret="s",
            polymarket_api_passphrase="p",
            polymarket_private_key="0xk",
        )
        assert env.has_credentials() is True
        assert load_settings(env, trading_values).mode.value == "V2"

    def test_v2_still_refuses_missing_venue_keys(
        self, trading_values: dict[str, str]
    ) -> None:
        env = ArcSettings(_env_file=None, mode="V2", chainlink_api_key="ck")
        with pytest.raises(ConfigInvariantError, match="requires credentials"):
            load_settings(env, trading_values)


class TestTheStrategyPathIsProviderBlind:
    """Changing providers must require configuration only; no strategy code may change.

    Enforced by reading the import graph rather than by grepping for a string, so a
    module that reached the provider through an alias would still be caught.
    """

    def _imports(self, path: Path) -> set[str]:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        names: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                names.add(node.module)
            elif isinstance(node, ast.Import):
                names.update(alias.name for alias in node.names)
        return names

    def _targets(self, source_root: Path) -> list[Path]:
        paths: list[Path] = []
        for entry in PROVIDER_BLIND:
            target = source_root / entry
            paths.extend([target] if target.is_file() else sorted(target.glob("*.py")))
        return paths

    def test_no_strategy_module_imports_a_provider(self, source_root: Path) -> None:
        targets = self._targets(source_root)
        assert targets, "the blind-list resolved to nothing; the paths are stale"
        offenders = [
            str(p.relative_to(source_root))
            for p in targets
            if {"arc.market.providers", "arc.market.feed"} & self._imports(p)
        ]
        assert not offenders, f"provider-aware modules: {offenders}"

    def test_none_of_them_mention_a_provider_by_name(self, source_root: Path) -> None:
        """Not even in a branch. A provider check in the strategy path IS the leak."""
        for path in self._targets(source_root):
            text = path.read_text(encoding="utf-8").upper()
            assert "TWAP_PROVIDER" not in text, path
            assert "RTDS" not in text, path


class TestTheProviderNameAppearsInNoEngine:
    """A21, executed as a test rather than trusted as a guideline.

    The literal sweep the specification names:

        grep -ri "rtds\\|chainlink" arc/strategy/ arc/windows/ arc/decision/ \\
            arc/risk/ arc/execution/

    must return nothing. Deliberately cruder than the import walk above and kept
    alongside it: this one also catches a provider name in a docstring, a comment
    or a log line. Prose is not harmless here — a comment naming the active
    provider is the first step of someone adding the branch it describes, and once
    an engine can tell which provider is live, "configuration only" has stopped
    being true.
    """

    ENGINES = ("strategy", "windows", "decision", "risk", "execution")
    NAMES = ("rtds", "chainlink")

    def test_no_engine_source_file_contains_a_provider_name(
        self, source_root: Path
    ) -> None:
        offenders: list[str] = []
        for package in self.ENGINES:
            directory = source_root / "arc" / package
            assert directory.is_dir(), f"{directory} is missing; the A21 list is stale"
            for path in sorted(directory.rglob("*.py")):
                lowered = path.read_text(encoding="utf-8").lower()
                offenders.extend(
                    f"{path.relative_to(source_root)}: {name}"
                    for name in self.NAMES
                    if name in lowered
                )
        assert not offenders, f"A21 provider-name leak: {offenders}"
