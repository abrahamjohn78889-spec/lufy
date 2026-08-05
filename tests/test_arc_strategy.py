"""The ARC TWAP + Locked Buffer strategy: sizing, purity, and what it refuses.

The trigger comparison is not tested here — the window already made it, and
test_activation.py owns it. What is tested is that this strategy does not repeat it,
and that its arithmetic can never overspend.
"""

from __future__ import annotations

import ast
from decimal import Decimal
from pathlib import Path

import pytest

from arc.domain.enums import Direction
from arc.strategy.arc_twap_locked_buffer import STRATEGY_ID, ArcTwapLockedBuffer
from arc.strategy.protocol import StrategyContext

BUDGET = Decimal("25.00")


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
        "position_notional_usd": BUDGET,
        "tick_size": Decimal("0.01"),
        "min_tradable_size": Decimal("5"),
    }
    base.update(overrides)
    return StrategyContext(**base)  # type: ignore[arg-type]


@pytest.fixture
def strategy() -> ArcTwapLockedBuffer:
    return ArcTwapLockedBuffer()


class TestSizing:
    def test_a_normal_context_produces_a_floored_price_and_size(
        self, strategy: ArcTwapLockedBuffer
    ) -> None:
        decision = strategy.decide(_context(quote_price=Decimal("0.7043")))
        assert decision.act
        # 0.7043 floors to 0.70 at a 0.01 tick; 25.00 / 0.70 floors to 35 shares.
        assert decision.limit_price == Decimal("0.70")
        assert decision.size == Decimal("35")

    def test_the_notional_never_exceeds_the_budget(
        self, strategy: ArcTwapLockedBuffer
    ) -> None:
        """ROUND_FLOOR throughout, so the spend lands at or under budget and never
        one share's worth over it."""
        for cents in range(6, 100):
            price = Decimal(cents) / Decimal(100)
            decision = strategy.decide(_context(quote_price=price))
            if not decision.act:
                continue
            assert decision.size * decision.limit_price <= BUDGET, price

    def test_the_price_is_quantized_before_the_size_is_derived(
        self, strategy: ArcTwapLockedBuffer
    ) -> None:
        """Defect D2. Sizing off an unquantized price and flooring afterwards leaves
        the pair inconsistent and the venue rejects it."""
        decision = strategy.decide(_context(quote_price=Decimal("0.7999")))
        assert decision.limit_price == Decimal("0.79")
        assert decision.size == Decimal("25.00") // Decimal("0.79")

    def test_the_size_is_a_whole_number_of_shares(
        self, strategy: ArcTwapLockedBuffer
    ) -> None:
        decision = strategy.decide(_context(quote_price=Decimal("0.33")))
        assert decision.size == decision.size.to_integral_value()

    def test_the_minimum_is_a_floor_not_a_lot_size(
        self, strategy: ArcTwapLockedBuffer
    ) -> None:
        """A min_tradable_size of 5 must not quantize a 35-share order down to 35's
        nearest multiple of 5 by accident, nor a 19-share one down to 15."""
        decision = strategy.decide(
            _context(quote_price=Decimal("0.70"), min_tradable_size=Decimal("5"))
        )
        assert decision.size == Decimal("35")
        decision = strategy.decide(
            _context(
                quote_price=Decimal("0.70"),
                position_notional_usd=Decimal("13.50"),
                min_tradable_size=Decimal("5"),
            )
        )
        assert decision.size == Decimal("19")


class TestRefusals:
    def test_a_non_positive_quote_is_refused_not_divided_by(
        self, strategy: ArcTwapLockedBuffer
    ) -> None:
        for price in (Decimal("0"), Decimal("-0.5")):
            decision = strategy.decide(_context(quote_price=price))
            assert not decision.act
            assert decision.size == Decimal("0")
            assert "no usable quote" in decision.reason

    def test_a_quote_below_one_tick_is_refused(self, strategy: ArcTwapLockedBuffer) -> None:
        decision = strategy.decide(_context(quote_price=Decimal("0.004")))
        assert not decision.act
        assert "floors to zero" in decision.reason

    def test_a_budget_too_small_for_the_minimum_is_refused(
        self, strategy: ArcTwapLockedBuffer
    ) -> None:
        decision = strategy.decide(
            _context(position_notional_usd=Decimal("2.00"), min_tradable_size=Decimal("5"))
        )
        assert not decision.act
        assert "below the 5 minimum" in decision.reason

    def test_a_refusal_still_reports_the_price_it_computed(
        self, strategy: ArcTwapLockedBuffer
    ) -> None:
        """So the operator can see WHY the size came out short."""
        decision = strategy.decide(_context(position_notional_usd=Decimal("2.00")))
        assert decision.limit_price == Decimal("0.70")


class TestPurity:
    def test_the_same_context_produces_the_same_decision_every_time(
        self, strategy: ArcTwapLockedBuffer
    ) -> None:
        context = _context(quote_price=Decimal("0.6137"))
        first = strategy.decide(context)
        for _ in range(200):
            assert strategy.decide(context) == first

    def test_two_instances_agree(self) -> None:
        context = _context(quote_price=Decimal("0.6137"))
        assert ArcTwapLockedBuffer().decide(context) == ArcTwapLockedBuffer().decide(context)

    def test_the_strategy_has_no_attribute_storage(self) -> None:
        """__slots__ = () makes "remembers nothing between markets" a property of the
        object layout rather than of the code remembering not to assign (A11)."""
        assert ArcTwapLockedBuffer.__slots__ == ()
        with pytest.raises(AttributeError):
            ArcTwapLockedBuffer().state = 1  # type: ignore[attr-defined]

    def test_the_decision_ignores_the_signal_twap_and_the_trigger(
        self, strategy: ArcTwapLockedBuffer
    ) -> None:
        """The window already made the comparison. Repeating it here would let the
        frozen numbers and the live ones disagree while looking healthy."""
        baseline = strategy.decide(_context())
        moved = strategy.decide(
            _context(signal_twap=Decimal("99999"), locked_trigger=Decimal("1"))
        )
        assert moved == baseline

    def test_the_decision_ignores_the_direction(self, strategy: ArcTwapLockedBuffer) -> None:
        """Direction selects which book the CALLER quotes, not how the size is
        derived. A strategy that sized differently per side would make UP and DOWN
        two strategies wearing one id."""
        up = strategy.decide(_context(direction=Direction.UP))
        down = strategy.decide(_context(direction=Direction.DOWN))
        assert up == down


class TestTheSourceCannotReachOutside:
    """AST, not grep: a comment mentioning `time` must not fail, and `import time`
    inside a function must not pass."""

    @staticmethod
    def _tree() -> ast.Module:
        path = Path(ArcTwapLockedBuffer.__module__.replace(".", "/") + ".py")
        return ast.parse(path.read_text(encoding="utf-8"))

    def test_the_strategy_imports_nothing_that_performs_io(self) -> None:
        forbidden = {
            "time",
            "random",
            "asyncio",
            "socket",
            "httpx",
            "sqlite3",
            "datetime",
            "os",
            "logging",
            "arc.storage",
            "arc.clock",
        }
        imported: set[str] = set()
        for node in ast.walk(self._tree()):
            if isinstance(node, ast.Import):
                imported.update(a.name for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
        assert imported.isdisjoint(forbidden), imported & forbidden

    def test_the_strategy_calls_no_clock_or_randomness(self) -> None:
        forbidden = {"time", "monotonic", "now", "uuid4", "random", "randint", "sleep"}
        called: set[str] = set()
        for node in ast.walk(self._tree()):
            if isinstance(node, ast.Call):
                target = node.func
                if isinstance(target, ast.Name):
                    called.add(target.id)
                elif isinstance(target, ast.Attribute):
                    called.add(target.attr)
        assert called.isdisjoint(forbidden), called & forbidden


class TestDescription:
    def test_the_default_is_pinned_and_not_disableable(
        self, strategy: ArcTwapLockedBuffer
    ) -> None:
        description = strategy.describe()
        assert description.strategy_id == STRATEGY_ID == "arc_twap_locked_buffer"
        assert description.pinned
        assert not description.disableable

    def test_describe_does_not_depend_on_any_input(
        self, strategy: ArcTwapLockedBuffer
    ) -> None:
        assert strategy.describe() == strategy.describe()
