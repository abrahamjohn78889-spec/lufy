"""Presets: named CONFIG values for the default strategy, not separate strategies.

`Aggressive 3s` and `Conservative 15s` are two buffer/window sets for
arc_twap_locked_buffer (A17). They are deliberately NOT registry entries. If they
were, the API would report three strategies where one exists, and the operator
would believe a preset switch changed the algorithm when it only changed numbers.

Every preset is expressed as raw config strings and applied through the ordinary
`build_trading_config` validation path, so a preset gets exactly the same thirteen
fatal invariant checks as a hand-edited setting. A preset that bypassed validation
would be the one way an invalid configuration could reach the engine.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

__all__ = ["PRESETS", "Preset", "preset_ids", "preset_values"]


@dataclass(frozen=True, slots=True)
class Preset:
    """One named configuration of the default strategy.

    `values` holds only the keys the preset changes. It is merged over the current
    configuration rather than replacing it, so applying a preset cannot silently
    reset the entry band, the cancel lead or the loss limits — settings a preset
    has no opinion about.
    """

    preset_id: str
    name: str
    description: str
    values: dict[str, str]


# The buffers below are shaped by TWAP inertia (A7): moving the 300-second mean one
# buffer needs a BTC deviation of buffer x (300 / offset), so the same buffer is a
# 20x requirement on the 15s window and a 100x requirement on the 3s window. A
# preset that used one buffer across all five windows would be aggressive at 15s
# and unreachable at 3s while reading as uniform.
_AGGRESSIVE_3S: Final[Preset] = Preset(
    preset_id="aggressive_3s",
    name="Aggressive 3s",
    description=(
        "Trades the latest, best-informed window only. Smallest buffer, so the "
        "trigger is nearest — at 3 seconds a buffer of 0.50 still requires a $50 "
        "BTC move to satisfy."
    ),
    values={
        "execution_windows": "3",
        "buffers": "3:0.50",
    },
)

_CONSERVATIVE_15S: Final[Preset] = Preset(
    preset_id="conservative_15s",
    name="Conservative 15s",
    description=(
        "Trades the earliest window only, with a wide buffer. At 15 seconds a "
        "buffer of 3.00 requires a $60 BTC move, so the signal has to be "
        "substantial before the window acts."
    ),
    values={
        "execution_windows": "15",
        "buffers": "15:3.00",
    },
)

PRESETS: Final[tuple[Preset, ...]] = (_AGGRESSIVE_3S, _CONSERVATIVE_15S)


def preset_ids() -> tuple[str, ...]:
    return tuple(p.preset_id for p in PRESETS)


def preset_values(preset_id: str, current: dict[str, str]) -> dict[str, str]:
    """Merge a preset over the current raw values. Returns a NEW dict.

    Merged rather than substituted, and copied rather than mutated: applying a
    preset must not damage the caller's configuration if the merged result then
    fails validation. The caller passes the result to `build_trading_config` and
    keeps the old configuration active if it raises.
    """
    for preset in PRESETS:
        if preset.preset_id == preset_id:
            return dict(current) | dict(preset.values)
    raise KeyError(preset_id)
