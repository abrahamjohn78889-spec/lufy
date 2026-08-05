"""The strategy registry: exactly one entry, and it cannot be removed.

This exists as an architectural boundary, not as a growth area. Additional
strategies are deferred behind a gate of 100+ real markets of V1 data (A17), and
until then the registry holds one real strategy and nothing else — no stubs, no
placeholder ids, no dead branches for a second plugin that does not exist.

The default is PINNED: registering over it, or unregistering it, raises. The
failure that prevents is a configuration change, an API call or a future
refactor leaving the process with zero registered strategies, at which point
every fired window would be skipped and the log would report five ordinary
non-signals per market rather than a missing strategy.
"""

from __future__ import annotations

from typing import Final

from arc.strategy.arc_twap_locked_buffer import STRATEGY_ID, ArcTwapLockedBuffer
from arc.strategy.protocol import Strategy, StrategyDescription

__all__ = ["DEFAULT_STRATEGY_ID", "StrategyRegistry", "default_registry"]

DEFAULT_STRATEGY_ID: Final[str] = STRATEGY_ID


class StrategyRegistry:
    """Holds the registered strategies. Instantiated, never module-global.

    A module-level registry would be per-process mutable state that any import
    could reach and any test could leave dirty (A11). The runtime builds one and
    passes it to the Decision Engine.
    """

    __slots__ = ("_pinned", "_strategies")

    def __init__(self) -> None:
        self._strategies: dict[str, Strategy] = {}
        self._pinned: set[str] = set()

    def register(self, strategy: Strategy, *, pinned: bool = False) -> None:
        """Add a strategy. Refuses a duplicate id and refuses to shadow a pin.

        Checked with isinstance against the runtime_checkable protocol, so a
        malformed plugin fails here rather than at the first fired window — which
        in a five-minute market is up to five minutes of silent non-trading.
        """
        if not isinstance(strategy, Strategy):
            raise ValueError(
                f"{type(strategy).__name__} does not satisfy the Strategy protocol; "
                "it must provide describe() and decide()"
            )
        description = strategy.describe()
        strategy_id = description.strategy_id
        if not strategy_id:
            raise ValueError("a strategy must declare a non-empty strategy_id")
        if strategy_id in self._pinned:
            raise ValueError(
                f"{strategy_id!r} is pinned and cannot be replaced. Replacing the "
                "default would silently change what every window trades."
            )
        if strategy_id in self._strategies:
            raise ValueError(f"{strategy_id!r} is already registered")
        self._strategies[strategy_id] = strategy
        if pinned or description.pinned:
            self._pinned.add(strategy_id)

    def unregister(self, strategy_id: str) -> None:
        """Remove a strategy. A pinned one cannot be removed."""
        if strategy_id in self._pinned:
            raise ValueError(
                f"{strategy_id!r} is pinned and cannot be unregistered. A registry "
                "with no strategies would skip every fired window in silence."
            )
        if strategy_id not in self._strategies:
            raise KeyError(strategy_id)
        del self._strategies[strategy_id]

    def get(self, strategy_id: str) -> Strategy:
        if strategy_id not in self._strategies:
            raise KeyError(strategy_id)
        return self._strategies[strategy_id]

    @property
    def default(self) -> Strategy:
        """The pinned default. Always present, so this never returns None."""
        return self._strategies[DEFAULT_STRATEGY_ID]

    def ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._strategies))

    def describe_all(self) -> tuple[StrategyDescription, ...]:
        """For GET /strategies. Ordered by id so the response is deterministic."""
        return tuple(self._strategies[k].describe() for k in sorted(self._strategies))

    def is_pinned(self, strategy_id: str) -> bool:
        return strategy_id in self._pinned

    def __len__(self) -> int:
        return len(self._strategies)

    def __contains__(self, strategy_id: object) -> bool:
        return strategy_id in self._strategies


def default_registry() -> StrategyRegistry:
    """A registry holding the one strategy that exists, pinned."""
    registry = StrategyRegistry()
    registry.register(ArcTwapLockedBuffer(), pinned=True)
    return registry
