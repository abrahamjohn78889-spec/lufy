"""The strategy protocol: what a strategy may see, and what it may say.

Structural assertions only. Whether the ARC strategy sizes correctly is
test_arc_strategy.py's job; this file asserts the shape of the boundary.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError, fields
from decimal import Decimal

import pytest

from arc.domain.enums import Direction
from arc.strategy.protocol import (
    Strategy,
    StrategyContext,
    StrategyDecision,
    StrategyDescription,
)


def _context(**overrides: object) -> StrategyContext:
    base: dict[str, object] = {
        "market_slug": "btc-updown-5m-1754400000",
        "close_ts": 1754400300,
        "offset_seconds": 3,
        "direction": Direction.UP,
        "opening_twap": Decimal("64000"),
        "ptb": Decimal("64000"),
        "buffer": Decimal("1.00"),
        "locked_trigger": Decimal("64001.00"),
        "signal_twap": Decimal("64002.00"),
        "quote_price": Decimal("0.70"),
        "position_notional_usd": Decimal("25.00"),
        "tick_size": Decimal("0.01"),
        "min_tradable_size": Decimal("5"),
    }
    base.update(overrides)
    return StrategyContext(**base)  # type: ignore[arg-type]


class TestTheContextIsSealed:
    def test_the_context_cannot_be_mutated(self) -> None:
        context = _context()
        with pytest.raises(FrozenInstanceError):
            context.signal_twap = Decimal("1")  # type: ignore[misc]

    def test_the_context_does_not_carry_the_settlement_twap(self) -> None:
        """A6. The venue's 30s TWAP is the OUTCOME; a strategy able to read it
        would be fitting to the answer."""
        names = {f.name for f in fields(StrategyContext)}
        assert not any("settlement" in n for n in names), names

    def test_the_context_carries_no_mutable_reference(self) -> None:
        """No store, no clock, no market, no config object.

        A strategy holding any of these would stop being a pure function of its
        context, and a replay of one observation stream could then produce two
        different decisions.
        """
        names = {f.name for f in fields(StrategyContext)}
        forbidden = {"market", "store", "clock", "config", "trading", "logger", "engine"}
        assert names.isdisjoint(forbidden), names & forbidden

    def test_every_context_field_is_a_value_not_an_object(self) -> None:
        context = _context()
        for field in fields(StrategyContext):
            value = getattr(context, field.name)
            assert isinstance(value, str | int | Decimal | Direction), field.name


class TestTheDecisionIsAProposal:
    def test_the_decision_cannot_be_mutated(self) -> None:
        decision = StrategyDecision(act=True, limit_price=Decimal("0.7"), size=Decimal("35"))
        with pytest.raises(FrozenInstanceError):
            decision.act = False  # type: ignore[misc]

    def test_a_refusal_carries_words_not_a_denial_reason(self) -> None:
        """A strategy declining is not a risk denial. Conflating the two would make
        the rejection log claim a gate fired when none did."""
        decision = StrategyDecision(
            act=False, limit_price=Decimal("0"), size=Decimal("0"), reason="no usable quote"
        )
        assert isinstance(decision.reason, str)
        annotations = {f.name: f.type for f in fields(StrategyDecision)}
        assert "DenialReason" not in str(annotations["reason"])

    def test_the_decision_cannot_submit_anything(self) -> None:
        """No order id, no venue field, no callback. A proposal, never an action."""
        names = {f.name for f in fields(StrategyDecision)}
        assert names == {"act", "limit_price", "size", "reason"}


class TestTheProtocolIsCheckableAtRegistrationTime:
    def test_an_object_with_both_methods_satisfies_the_protocol(self) -> None:
        class Minimal:
            def describe(self) -> StrategyDescription:
                return StrategyDescription(strategy_id="x", name="X", description="")

            def decide(self, context: StrategyContext) -> StrategyDecision:
                return StrategyDecision(act=False, limit_price=Decimal("0"), size=Decimal("0"))

        assert isinstance(Minimal(), Strategy)

    def test_an_object_missing_decide_does_not(self) -> None:
        """Checked at registration rather than at the first fired window, which in a
        five-minute market is up to five minutes of silent non-trading."""

        class Broken:
            def describe(self) -> StrategyDescription:
                return StrategyDescription(strategy_id="x", name="X", description="")

        assert not isinstance(Broken(), Strategy)


class TestTheDescriptionCarriesItsOwnPinning:
    def test_pinned_and_disableable_travel_with_the_strategy(self) -> None:
        """Held on the description, not in the registry, so "the default cannot be
        turned off" cannot be lost by a registry rewrite (A17)."""
        names = {f.name for f in fields(StrategyDescription)}
        assert {"pinned", "disableable"} <= names

    def test_a_description_defaults_to_unpinned_and_disableable(self) -> None:
        description = StrategyDescription(strategy_id="x", name="X", description="")
        assert not description.pinned
        assert description.disableable
