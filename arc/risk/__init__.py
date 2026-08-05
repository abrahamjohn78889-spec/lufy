"""Risk: the single place a trade can be authorised.

Nothing outside this package decides whether an order may exist. The A8
submission boundary lives here, so a caller that forgets to consult the runtime
flag still cannot submit.
"""

from __future__ import annotations

from arc.risk.engine import GATE_ORDER, RiskContext, RiskEngine, RiskVerdict
from arc.risk.limits import RiskLimits, limits_from_trading

__all__ = [
    "GATE_ORDER",
    "RiskContext",
    "RiskEngine",
    "RiskLimits",
    "RiskVerdict",
    "limits_from_trading",
]
