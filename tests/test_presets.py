"""Presets: config values for the one strategy, not strategies of their own (A17)."""

from __future__ import annotations

from decimal import Decimal

import pytest
from conftest import VALID_TRADING_VALUES

from arc.config import build_trading_config
from arc.strategy.presets import PRESETS, preset_ids, preset_values
from arc.strategy.registry import default_registry


class TestAPresetIsConfigurationOnly:
    def test_no_preset_is_a_registry_entry(self) -> None:
        """Registering them would make the API report three strategies where one
        exists, and a preset switch would read as an algorithm change."""
        registered = set(default_registry().ids())
        assert registered.isdisjoint(set(preset_ids()))

    def test_the_two_shipped_presets_are_the_documented_pair(self) -> None:
        assert preset_ids() == ("aggressive_3s", "conservative_15s")

    def test_a_preset_changes_only_windows_and_buffers(self) -> None:
        """A preset has no opinion about the entry band, the cancel lead or the loss
        limits, and must not silently reset them."""
        for preset in PRESETS:
            assert set(preset.values) == {"execution_windows", "buffers"}

    def test_no_preset_declares_a_strategy_id(self) -> None:
        for preset in PRESETS:
            assert "strategy" not in preset.values


class TestMerging:
    def test_a_preset_merges_over_the_current_values(self) -> None:
        merged = preset_values("aggressive_3s", dict(VALID_TRADING_VALUES))
        assert merged["execution_windows"] == "3"
        assert merged["entry_price_max"] == VALID_TRADING_VALUES["entry_price_max"]

    def test_merging_does_not_mutate_the_caller_dict(self) -> None:
        """So a merged result that fails validation leaves the running configuration
        intact."""
        current = dict(VALID_TRADING_VALUES)
        before = dict(current)
        preset_values("conservative_15s", current)
        assert current == before

    def test_an_unknown_preset_raises(self) -> None:
        with pytest.raises(KeyError):
            preset_values("nope", dict(VALID_TRADING_VALUES))


class TestEveryPresetSurvivesTheOrdinaryValidationPath:
    @pytest.mark.parametrize("preset_id", preset_ids())
    def test_the_preset_builds_a_valid_trading_config(self, preset_id: str) -> None:
        """Applied through build_trading_config, so a preset gets exactly the same
        fatal invariant checks as a hand-edited setting."""
        config = build_trading_config(preset_values(preset_id, dict(VALID_TRADING_VALUES)))
        assert len(config.execution_windows) == 1
        assert set(config.buffers) == set(config.execution_windows)

    def test_aggressive_trades_the_3s_window(self) -> None:
        config = build_trading_config(
            preset_values("aggressive_3s", dict(VALID_TRADING_VALUES))
        )
        assert config.execution_windows == (3,)
        assert config.buffer_for(3) == Decimal("0.50")

    def test_conservative_trades_the_15s_window(self) -> None:
        config = build_trading_config(
            preset_values("conservative_15s", dict(VALID_TRADING_VALUES))
        )
        assert config.execution_windows == (15,)
        assert config.buffer_for(15) == Decimal("3.00")


class TestThePresetsAreInterpretableSideBySide:
    def test_the_implied_btc_move_is_what_makes_them_comparable(self) -> None:
        """A7 inertia. The same buffer is a 20x requirement at 15s and a 100x one at
        3s, so the buffer numbers alone say nothing about aggression."""
        aggressive = build_trading_config(
            preset_values("aggressive_3s", dict(VALID_TRADING_VALUES))
        )
        conservative = build_trading_config(
            preset_values("conservative_15s", dict(VALID_TRADING_VALUES))
        )
        assert aggressive.implied_btc_move(3) == Decimal("50.00")
        assert conservative.implied_btc_move(15) == Decimal("60.00")
        # And the "aggressive" preset really is the nearer trigger of the two.
        assert aggressive.implied_btc_move(3) < conservative.implied_btc_move(15)
