"""Wallet and portfolio, read-only. Official SDK fields or nothing.

Every value here comes from one of exactly two sources: an official, documented
`polymarket-client` call, or ARC's own persisted ledger. Nothing is estimated,
interpolated, or derived from a heuristic. A field with no official source is
reported as `None` and the dashboard renders it as UNAVAILABLE — a fabricated
buying-power number is worse than a blank one, because an operator sizes real
positions against it and it looks exactly as authoritative as a real balance.

Aggregation is not derivation. Summing the venue's own `current_value` across the
venue's own positions reports the venue's numbers; inventing "available funds =
balance minus a guess at pending margin" would not.

V1 has no venue account, so every venue-sourced field is unavailable and the
ledger fields carry the whole panel. That asymmetry is the point: paper PnL must
never be presented as an account balance.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING, Final, Protocol

from arc.domain.enums import Mode
from arc.storage.store import Store

if TYPE_CHECKING:  # pragma: no cover - annotations only
    from polymarket import AsyncSecureClient

__all__ = [
    "STATUS_DISCONNECTED",
    "LedgerStats",
    "LiveWallet",
    "PaperWallet",
    "WalletReader",
    "WalletSnapshot",
    "build_wallet",
    "ledger_stats",
]

_ZERO: Final[Decimal] = Decimal("0")

# USDC exposes six decimals and `get_balance_allowance` returns base units. Scaling
# by anything else silently reports a balance off by a factor of a thousand, which
# reads as a plausible account size rather than as an error.
_COLLATERAL_DECIMALS: Final[Decimal] = Decimal(10) ** 6

_DAY_SECONDS: Final[float] = 86400.0

_STATUS_CONNECTED: Final[str] = "CONNECTED"
_STATUS_PAPER: Final[str] = "PAPER (no venue account)"
# Public because the risk engine's wallet gate compares against it. One spelling,
# in one place: a second literal elsewhere would silently stop matching the day
# this string changed, and the gate would pass on a disconnected wallet.
STATUS_DISCONNECTED: Final[str] = "DISCONNECTED"


@dataclass(frozen=True, slots=True)
class LedgerStats:
    """Trading history as ARC recorded it. Survives restart; independent of the venue."""

    realized_today: Decimal
    realized_run: Decimal
    realized_lifetime: Decimal
    largest_win: Decimal
    largest_loss: Decimal
    winning_streak: int
    losing_streak: int
    markets_settled: int
    wins: int
    losses: int


@dataclass(frozen=True, slots=True)
class WalletSnapshot:
    """One render of the Wallet Panel. `None` means no official source exists."""

    # Connection
    address: str
    status: str
    network: str
    provider: str
    credentialed: bool
    # Balances
    available_balance: Decimal | None
    reserved_balance: Decimal | None
    balance_in_open_positions: Decimal | None
    buying_power: Decimal | None
    total_account_value: Decimal | None
    # Trading
    current_exposure: Decimal | None
    current_position_value: Decimal | None
    pending_position_value: Decimal
    open_position_count: int
    # PnL
    unrealized_pnl: Decimal | None
    ledger: LedgerStats


class WalletReader(Protocol):
    """The dashboard's single door to account state. One call, one snapshot."""

    async def snapshot(self, now: float, *, run_start: float) -> WalletSnapshot: ...


def ledger_stats(store: Store, now: float, run_start: float) -> LedgerStats:
    """Realized results from ARC's settlements table.

    Read from storage rather than accumulated in memory so a restart cannot reset
    a losing day: the daily-loss gate and this panel must agree on the same number
    after a crash as before it.
    """
    history = store.settlement_history(limit=10_000)
    day_start = now - _DAY_SECONDS

    today = _ZERO
    session = _ZERO
    lifetime = _ZERO
    largest_win = _ZERO
    largest_loss = _ZERO
    wins = 0
    losses = 0

    for settlement in history:
        pnl = settlement.pnl
        lifetime += pnl
        if settlement.settled_at >= day_start:
            today += pnl
        if settlement.settled_at >= run_start:
            session += pnl
        if pnl > largest_win:
            largest_win = pnl
        if pnl < largest_loss:
            largest_loss = pnl
        if pnl > _ZERO:
            wins += 1
        elif pnl < _ZERO:
            losses += 1

    # settlement_history is newest-first, so the current streak is the leading run.
    # A settlement with zero PnL (no fill in that market) ends neither streak: it was
    # not a loss, and counting it as one would trip the consecutive-loss limit on a
    # quiet hour.
    winning = 0
    losing = 0
    for settlement in history:
        if settlement.pnl > _ZERO:
            if losing:
                break
            winning += 1
        elif settlement.pnl < _ZERO:
            if winning:
                break
            losing += 1

    return LedgerStats(
        realized_today=today,
        realized_run=session,
        realized_lifetime=lifetime,
        largest_win=largest_win,
        largest_loss=largest_loss,
        winning_streak=winning,
        losing_streak=losing,
        markets_settled=len(history),
        wins=wins,
        losses=losses,
    )


def _pending_value(store: Store) -> tuple[Decimal, int]:
    """Notional still resting on the book, from ARC's own order rows."""
    live = store.live_orders()
    total = sum((order.price * order.remaining_size for order in live), _ZERO)
    return total, len(live)


class PaperWallet:
    """V1. No venue account exists, so no venue field is reported."""

    __slots__ = ("_store",)

    def __init__(self, store: Store) -> None:
        self._store = store

    async def snapshot(self, now: float, *, run_start: float) -> WalletSnapshot:
        pending, open_orders = _pending_value(self._store)
        return WalletSnapshot(
            address="",
            status=_STATUS_PAPER,
            network="",
            provider=Mode.V1.value,
            credentialed=False,
            available_balance=None,
            reserved_balance=None,
            balance_in_open_positions=None,
            buying_power=None,
            total_account_value=None,
            current_exposure=None,
            current_position_value=None,
            pending_position_value=pending,
            open_position_count=open_orders,
            unrealized_pnl=None,
            ledger=ledger_stats(self._store, now, run_start),
        )


class LiveWallet:
    """V2. Official SDK reads only, and a failed read reports DISCONNECTED.

    A venue error must not blank the panel to zeros. Zero balance and unknown
    balance are different facts, and an operator who reads the first as the second
    stops a run that had nothing wrong with it — or worse, the reverse.
    """

    __slots__ = ("_client", "_store")

    def __init__(self, client: AsyncSecureClient, store: Store) -> None:
        self._client = client
        self._store = store

    async def snapshot(self, now: float, *, run_start: float) -> WalletSnapshot:
        stats = ledger_stats(self._store, now, run_start)
        pending, open_orders = _pending_value(self._store)
        address = str(self._client.wallet)
        network = self._client.environment.name
        credentialed = self._client.credentials is not None

        status = _STATUS_CONNECTED
        collateral: Decimal | None = None
        try:
            allowance = await self._client.get_balance_allowance(asset_type="COLLATERAL")
            collateral = Decimal(allowance.balance) / _COLLATERAL_DECIMALS
        except Exception:
            status = STATUS_DISCONNECTED

        account_value: Decimal | None = None
        try:
            values = await self._client.get_portfolio_values(user=address)
            account_value = next((v.value for v in values if v.value is not None), None)
        except Exception:
            status = STATUS_DISCONNECTED

        positions_value: Decimal | None = None
        unrealized: Decimal | None = None
        position_count = 0
        try:
            page = await self._client.list_positions(user=address).first_page()
            positions_value = sum((p.current_value or _ZERO for p in page.items), _ZERO)
            unrealized = sum((p.cash_pnl or _ZERO for p in page.items), _ZERO)
            position_count = len(page.items)
        except Exception:
            status = STATUS_DISCONNECTED

        return WalletSnapshot(
            address=address,
            status=status,
            network=network,
            provider="Polymarket CLOB",
            credentialed=credentialed,
            available_balance=collateral,
            # No official endpoint reports collateral reserved against resting orders.
            reserved_balance=None,
            balance_in_open_positions=positions_value,
            # Buying power is not an official field. The venue publishes collateral
            # balance; a "buying power" number would be ARC's own guess at what the
            # matching engine will accept, and the operator would size against it.
            buying_power=None,
            total_account_value=account_value,
            current_exposure=positions_value,
            current_position_value=positions_value,
            pending_position_value=pending,
            open_position_count=position_count or open_orders,
            unrealized_pnl=unrealized,
            ledger=stats,
        )


def build_wallet(mode: Mode, store: Store, client: AsyncSecureClient | None) -> WalletReader:
    """One reader per mode. V2 without a client is paper's reader, never a fake balance."""
    if mode is Mode.V2 and client is not None:
        return LiveWallet(client, store)
    return PaperWallet(store)
