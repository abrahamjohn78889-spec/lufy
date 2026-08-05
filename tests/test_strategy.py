"""The strategy registry: exactly one entry, pinned, and unremovable (A17)."""

from __future__ import annotations

from decimal import Decimal

import pytest

from arc.strategy.arc_twap_locked_buffer import ArcTwapLockedBuffer
from arc.strategy.protocol import (
    StrategyContext,
    StrategyDecision,
    StrategyDescription,
)
from arc.strategy.registry import (
    DEFAULT_STRATEGY_ID,
    StrategyRegistry,
    default_registry,
)


class _Second:
    """A second real strategy, used only to prove the registry's rules.

    Defined in the test rather than shipped in arc/strategy/: additional strategies
    are deferred behind a gate of 100+ real markets of V1 data, and a stub in the
    source tree would be exactly the placeholder that gate exists to prevent.
    """

    __slots__ = ()

    def describe(self) -> StrategyDescription:
        return StrategyDescription(strategy_id="second", name="Second", description="")

    def decide(self, context: StrategyContext) -> StrategyDecision:
        return StrategyDecision(act=False, limit_price=Decimal("0"), size=Decimal("0"))


class TestTheShippedRegistry:
    def test_it_holds_exactly_one_strategy(self) -> None:
        registry = default_registry()
        assert len(registry) == 1
        assert registry.ids() == (DEFAULT_STRATEGY_ID,)

    def test_that_one_strategy_is_the_arc_default(self) -> None:
        assert isinstance(default_registry().default, ArcTwapLockedBuffer)

    def test_the_default_is_pinned(self) -> None:
        assert default_registry().is_pinned(DEFAULT_STRATEGY_ID)

    def test_it_contains_no_placeholder_ids(self) -> None:
        """No stubs, no reserved names, no dead branches for a plugin that does not
        exist (A17)."""
        for strategy_id in default_registry().ids():
            lowered = strategy_id.lower()
            assert not any(
                marker in lowered
                for marker in ("stub", "todo", "placeholder", "example", "dummy", "test")
            ), strategy_id

    def test_two_registries_are_independent(self) -> None:
        """Not module-global. A module-level registry would be process state any
        import could reach and any test could leave dirty (A11)."""
        first, second = default_registry(), default_registry()
        first.register(_Second())
        assert "second" in first
        assert "second" not in second


class TestThePinCannotBeDefeated:
    def test_the_default_cannot_be_unregistered(self) -> None:
        """A registry with no strategies would skip every fired window in silence."""
        registry = default_registry()
        with pytest.raises(ValueError, match="pinned"):
            registry.unregister(DEFAULT_STRATEGY_ID)
        assert len(registry) == 1

    def test_the_default_cannot_be_shadowed_by_a_re_registration(self) -> None:
        registry = default_registry()
        with pytest.raises(ValueError, match="pinned"):
            registry.register(ArcTwapLockedBuffer())

    def test_the_default_is_still_reachable_after_a_refused_removal(self) -> None:
        registry = default_registry()
        with pytest.raises(ValueError):
            registry.unregister(DEFAULT_STRATEGY_ID)
        assert isinstance(registry.default, ArcTwapLockedBuffer)


class TestRegistration:
    def test_a_malformed_plugin_is_refused_at_registration(self) -> None:
        """Not at the first fired window, which in a five-minute market is up to five
        minutes of silent non-trading."""

        class NoDecide:
            def describe(self) -> StrategyDescription:
                return StrategyDescription(strategy_id="x", name="X", description="")

        with pytest.raises(ValueError, match="Strategy protocol"):
            StrategyRegistry().register(NoDecide())  # type: ignore[arg-type]

    def test_an_empty_strategy_id_is_refused(self) -> None:
        class Anonymous:
            def describe(self) -> StrategyDescription:
                return StrategyDescription(strategy_id="", name="", description="")

            def decide(self, context: StrategyContext) -> StrategyDecision:
                return StrategyDecision(act=False, limit_price=Decimal("0"), size=Decimal("0"))

        with pytest.raises(ValueError, match="non-empty strategy_id"):
            StrategyRegistry().register(Anonymous())

    def test_a_duplicate_id_is_refused(self) -> None:
        registry = StrategyRegistry()
        registry.register(_Second())
        with pytest.raises(ValueError, match="already registered"):
            registry.register(_Second())

    def test_an_unpinned_strategy_can_be_removed(self) -> None:
        """The boundary is real, not decorative: the mechanism works, it simply has
        exactly one occupant today."""
        registry = default_registry()
        registry.register(_Second())
        registry.unregister("second")
        assert registry.ids() == (DEFAULT_STRATEGY_ID,)

    def test_an_unknown_id_raises_on_get_and_unregister(self) -> None:
        registry = default_registry()
        with pytest.raises(KeyError):
            registry.get("missing")
        with pytest.raises(KeyError):
            registry.unregister("missing")


class TestDeterministicOrdering:
    def test_ids_are_sorted(self) -> None:
        registry = default_registry()
        registry.register(_Second())
        assert registry.ids() == tuple(sorted(registry.ids()))

    def test_describe_all_is_ordered_by_id(self) -> None:
        """So GET /strategies returns a byte-identical body for identical state."""
        registry = default_registry()
        registry.register(_Second())
        ids = [d.strategy_id for d in registry.describe_all()]
        assert ids == sorted(ids)
