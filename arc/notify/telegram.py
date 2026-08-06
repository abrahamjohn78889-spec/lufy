"""Telegram notifications. Twenty-five categories, each independently toggleable.

NOTIFICATION ONLY. Nothing here can start, stop, arm, disarm, submit, cancel or
configure anything, and there is no inbound path at all — no polling, no webhook, no
command handling. A chat message that could move money would make the Telegram
account a second set of trading credentials, held by whichever phone is signed in.

It reads the Signal Tank rather than being called from forty places. Every
significant action already publishes one event through the hub, so a subscriber sees
all of them and cannot drift the way a parallel notify() call per site would: the day
someone adds a log line and forgets the notify, this still sends it.

Delivery never blocks the runtime. The hub drops a slow subscriber's oldest entry,
and a failed send is logged and abandoned, because a Telegram outage must be unable
to delay an order.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Final

import httpx

from arc.logging_setup import log_event

if TYPE_CHECKING:  # pragma: no cover - annotations only
    from arc.runtime.events import EventHub

__all__ = [
    "CATEGORIES",
    "CATEGORY_LABELS",
    "EVENT_CATEGORY",
    "NOTIFY_PREFIX",
    "TelegramNotifier",
    "category_for",
    "category_settings",
    "notification_values",
]

# The twenty-five categories, in the order the Settings page lists them.
CATEGORY_LABELS: Final[dict[str, str]] = {
    "startup": "Startup",
    "shutdown": "Shutdown",
    "runtime_switched": "Runtime Switched",
    "trading_control": "Trading Started / Paused / Resumed / Stopped",
    "fatal_errors": "Fatal Errors",
    "warnings": "Warnings",
    "feed_disconnect": "Feed Disconnect",
    "feed_reconnect": "Feed Reconnect",
    "wallet_disconnect": "Wallet Disconnect",
    "wallet_reconnect": "Wallet Reconnect",
    "provider_reconnect": "Provider Reconnect",
    "ptb_frozen": "PTB Frozen",
    "window_open": "Window Open",
    "direction_frozen": "Direction Frozen",
    "trigger_fired": "Trigger Fired",
    "intent_created": "ExecutionIntent Created",
    "order_submitted": "Order Submitted",
    "partial_fill": "Partial Fill",
    "order_filled": "Order Filled",
    "cancelled": "Cancelled",
    "rejected": "Rejected",
    "reconciled": "Reconciled",
    "buffer_not_satisfied": "BUFFER_NOT_SATISFIED",
    "settlement": "Settlement",
    "daily_summary": "Daily Summary",
}

CATEGORIES: Final[tuple[str, ...]] = tuple(CATEGORY_LABELS)

# Which Signal Tank event feeds which category, keyed by the event label the emitting
# module actually logs. An event absent from this map falls back to its severity, so a
# log line added later still reaches the operator as a Warning or a Fatal Error rather
# than vanishing because nobody remembered to extend this table.
EVENT_CATEGORY: Final[dict[str, str]] = {
    "Runtime Started": "startup",
    "Runtime Stopped": "shutdown",
    "Runtime Switched": "runtime_switched",
    # The four operator gate changes. One category rather than four, because they
    # are one decision the operator keeps revising and an operator who muted
    # "Trading Paused" almost certainly meant to mute all four.
    "Trading Started": "trading_control",
    "Trading Paused": "trading_control",
    "Trading Resumed": "trading_control",
    "Trading Stopped": "trading_control",
    "Feed Disconnected": "feed_disconnect",
    "Feed Connected": "feed_reconnect",
    "Wallet Disconnected": "wallet_disconnect",
    "Wallet Reconnected": "wallet_reconnect",
    "Reconnecting": "provider_reconnect",
    "PTB Frozen": "ptb_frozen",
    "Window Open": "window_open",
    # Freezing IS the instant direction is determined, and the log line carries the
    # direction, the TWAP, the PTB, the buffer and the trigger.
    "Window Frozen": "direction_frozen",
    "Window No Direction": "direction_frozen",
    # Firing is the instant the locked trigger is crossed, which is a different
    # moment from the freeze and the one the operator is waiting on.
    "Window Fired": "trigger_fired",
    "Intent Created": "intent_created",
    "Orders Submitted": "order_submitted",
    "Partial Fill": "partial_fill",
    "Order Filled": "order_filled",
    "Sweep Complete": "cancelled",
    "Cancel Unknown": "cancelled",
    # An INDETERMINATE order resolved against the venue. Its own category because
    # it is the answer to "did that order exist", and a reconciliation the operator
    # never saw is a position they do not know they hold.
    "Order Reconciled": "reconciled",
    "Post-Only Would Cross": "rejected",
    "Intent Denied": "rejected",
    "Submission Skipped": "rejected",
    "Order Rejected": "rejected",
    "Order Unknown": "rejected",
    # A window whose trigger never crossed is BUFFER_NOT_SATISFIED. It is a strategy
    # outcome, not an error, which is why it is its own category rather than a warning.
    "Window No Signal": "buffer_not_satisfied",
    "Windows Expired": "buffer_not_satisfied",
    "Settlement Window": "settlement",
    "Market Closed": "settlement",
}

_SEVERITY_CATEGORY: Final[dict[str, str]] = {
    "FATAL": "fatal_errors",
    "ERROR": "fatal_errors",
    "WARNING": "warnings",
}

# Settings-table prefix. Toggles live beside the trading values so they survive a
# restart: a notification the operator switched off must stay off.
NOTIFY_PREFIX: Final[str] = "notify_"

_DAY_SECONDS: Final[float] = 86400.0

# Telegram's documented limit is 4096 characters per message and a longer body is
# rejected outright, so a long summary is truncated rather than lost.
_MAX_LEN: Final[int] = 4000

_TIMEOUT: Final[float] = 10.0

# The loop-breaker. A failed send logs a warning, that warning reaches the hub, and
# forwarding it would attempt another send that fails the same way.
_SELF_ENGINE: Final[str] = "Telegram"


def category_settings(stored: dict[str, str]) -> dict[str, bool]:
    """Which categories are on. Absent means on.

    Default-on so a category added in a later version starts reaching the operator
    instead of being silently muted on every existing installation.
    """
    return {
        name: stored.get(f"{NOTIFY_PREFIX}{name}", "true").strip().lower() != "false"
        for name in CATEGORIES
    }


def notification_values(flags: dict[str, bool]) -> dict[str, str]:
    """Serialise the toggles for the settings table. TEXT, like every other row."""
    return {
        f"{NOTIFY_PREFIX}{name}": ("true" if flags.get(name, True) else "false")
        for name in CATEGORIES
    }


def category_for(event: str, severity: str) -> str | None:
    """The category one Signal Tank line belongs to, or None to send nothing."""
    mapped = EVENT_CATEGORY.get(event)
    if mapped is not None:
        return mapped
    return _SEVERITY_CATEGORY.get(severity)


class TelegramNotifier:
    """One worker task, one chat, twenty-five toggles.

    Constructed even when unconfigured: `configured` is then False and `run` returns
    immediately, so the runtime needs no branch around it and an operator who fills
    the token in later gets notifications on the next restart without a code path
    that exists only when Telegram is set up.
    """

    __slots__ = (
        "_chat_id",
        "_enabled",
        "_flags",
        "_last_summary",
        "_logger",
        "_sent",
        "_thread_id",
        "_token",
    )

    def __init__(
        self,
        *,
        token: str,
        chat_id: str,
        flags: dict[str, bool],
        enabled: bool = True,
        thread_id: str = "",
        logger: logging.Logger | None = None,
    ) -> None:
        self._token = token
        self._chat_id = chat_id
        # The master switch, separate from the per-category toggles. An operator
        # silencing ARC for a maintenance window must not have to remember which of
        # twenty-five categories they turned off before turning them back on.
        self._enabled = enabled
        self._thread_id = thread_id.strip()
        # Held by reference, not copied: the Settings page edits this dict in place so
        # a toggle takes effect immediately. A copy would keep sending until restart,
        # and the operator who muted a noisy category would not believe the switch.
        self._flags = flags
        self._logger = logger
        self._last_summary = -1.0
        self._sent = 0

    @property
    def configured(self) -> bool:
        return bool(self._enabled and self._token and self._chat_id)

    @property
    def flags(self) -> dict[str, bool]:
        return self._flags

    @property
    def sent(self) -> int:
        """Messages successfully delivered. Displayed on the System page."""
        return self._sent

    def wants(self, category: str) -> bool:
        return self._flags.get(category, True)

    # ── sending ──────────────────────────────────────────────────────────────

    async def send(self, text: str) -> bool:
        """Post one message. Returns False on any failure and never raises.

        Errors are swallowed deliberately. An exception escaping here would
        propagate into whichever engine's event triggered it, turning a chat outage
        into a trading outage.
        """
        if not self.configured:
            return False
        url = f"https://api.telegram.org/bot{self._token}/sendMessage"
        payload: dict[str, Any] = {"chat_id": self._chat_id, "text": text[:_MAX_LEN]}
        # Only when set. Telegram rejects message_thread_id outright on a chat that
        # is not a forum, so sending it unconditionally would make every
        # notification fail on the ordinary chats most operators use.
        if self._thread_id:
            payload["message_thread_id"] = self._thread_id
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                response = await client.post(url, json=payload)
            response.raise_for_status()
        except Exception as exc:
            log_event(logging.WARNING, "Telegram Send Failed", str(exc), logger=self._logger)
            return False
        self._sent += 1
        return True

    # ── the worker ───────────────────────────────────────────────────────────

    async def run(self, hub: EventHub) -> None:
        """Forward matching Signal Tank events until cancelled.

        Subscribes to the hub rather than installing a second logging handler, so
        what Telegram sends and what the operator's console shows are the same
        stream. Two independent taps would eventually disagree about what happened.
        """
        if not self.configured:
            return
        queue = hub.subscribe()
        try:
            while True:
                text = self.render(await queue.get())
                if text is not None:
                    await self.send(text)
        finally:
            # Unconditional: a queue left in the hub is broadcast into forever.
            hub.unsubscribe(queue)

    def render(self, message: dict[str, Any]) -> str | None:
        """The message body for one hub message, or None if it is not to be sent."""
        if message.get("type") != "signal":
            return None
        data = message.get("data", {})
        if data.get("engine") == _SELF_ENGINE:
            return None
        category = category_for(str(data.get("event", "")), str(data.get("severity", "")))
        if category is None or not self.wants(category):
            return None
        detail = str(data.get("detail", ""))
        body = f"[ARC] {CATEGORY_LABELS[category]}\n{data.get('event', '')}"
        return f"{body}\n{detail}" if detail else body

    # ── daily summary ────────────────────────────────────────────────────────

    def summary_due(self, now: float) -> bool:
        """True at most once a day. Level-triggered, like everything else in ARC.

        Compared against elapsed time rather than driven by a scheduled callback: a
        process restarted between two firings would lose a scheduled call entirely,
        and the operator would notice only by the summary that never arrived.
        """
        if not self.configured or not self.wants("daily_summary"):
            return False
        if self._last_summary < 0.0:
            # First call arms the timer instead of firing. Otherwise every restart
            # sends a "daily" summary of a run that is one tick old. The sentinel is
            # negative rather than zero because zero is a legal monotonic reading.
            self._last_summary = now
            return False
        if now - self._last_summary < _DAY_SECONDS:
            return False
        self._last_summary = now
        return True

    async def send_summary(self, totals: dict[str, Any]) -> bool:
        lines = [f"{key.replace('_', ' ')}: {value}" for key, value in totals.items()]
        return await self.send("[ARC] Daily Summary\n" + "\n".join(lines))
