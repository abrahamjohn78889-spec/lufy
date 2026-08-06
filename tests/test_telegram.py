"""Telegram: twenty-one toggles, notification only, and no inbound path at all.

The failures pinned here are the ones that would be discovered at the wrong time: a
category mapped to an event name that no engine actually logs (so the operator is
never told), a chat outage that propagates into a trading engine, a feedback loop
where a send failure notifies about the send failure, and — the one that matters
most — any code path by which a chat message could reach the runtime.
"""

from __future__ import annotations

import ast
import asyncio
import contextlib
import logging
from pathlib import Path

from arc.logging_setup import log_event
from arc.notify.telegram import (
    CATEGORIES,
    CATEGORY_LABELS,
    EVENT_CATEGORY,
    NOTIFY_PREFIX,
    TelegramNotifier,
    category_for,
    category_settings,
    notification_values,
)
from arc.runtime.events import EventHub, attach

_ARC = Path(__file__).resolve().parent.parent / "arc"

_EXPECTED = (
    "Startup",
    "Shutdown",
    "Fatal Errors",
    "Warnings",
    "Feed Disconnect",
    "Feed Reconnect",
    "Wallet Disconnect",
    "Wallet Reconnect",
    "Provider Reconnect",
    "PTB Frozen",
    "Window Open",
    "Direction Frozen",
    "ExecutionIntent Created",
    "Order Submitted",
    "Partial Fill",
    "Order Filled",
    "Cancelled",
    "Rejected",
    "BUFFER_NOT_SATISFIED",
    "Settlement",
    "Daily Summary",
)


def _notifier(**flags: bool) -> TelegramNotifier:
    settings = dict.fromkeys(CATEGORIES, True)
    settings.update(flags)
    return TelegramNotifier(token="t", chat_id="c", flags=settings)


def _signal(event: str, severity: str = "INFO", engine: str = "Runtime") -> dict[str, object]:
    return {
        "type": "signal",
        "data": {"engine": engine, "severity": severity, "event": event, "detail": "d"},
    }


class TestTwentyOneCategories:
    def test_exactly_the_categories_the_spec_names(self) -> None:
        assert tuple(CATEGORY_LABELS.values()) == _EXPECTED
        assert len(CATEGORIES) == 21

    def test_each_is_independently_toggleable(self) -> None:
        notifier = _notifier(warnings=False)
        assert notifier.wants("warnings") is False
        # One off does not turn any other off.
        assert all(notifier.wants(c) for c in CATEGORIES if c != "warnings")

    def test_absent_setting_means_enabled(self) -> None:
        """A new category must default ON, not silently OFF.

        Defaulting to off would mean a category added in a later release stays
        invisible on every existing install until someone notices it never fires.
        """
        flags = category_settings({})
        assert set(flags) == set(CATEGORIES)
        assert all(flags.values())

    def test_settings_round_trip(self) -> None:
        flags = category_settings({f"{NOTIFY_PREFIX}rejected": "false"})
        assert flags["rejected"] is False
        stored = notification_values(flags)
        assert stored[f"{NOTIFY_PREFIX}rejected"] == "false"
        assert category_settings(stored) == flags

    def test_stored_values_are_strings(self) -> None:
        """The settings table stores TEXT. A bool would come back as "True"/"False"."""
        values = notification_values(dict.fromkeys(CATEGORIES, True))
        assert all(isinstance(v, str) for v in values.values())


class TestEveryMappedEventActuallyExists:
    def test_no_invented_event_names(self) -> None:
        """Every key of EVENT_CATEGORY must be a real log_event label in arc/.

        This is the failure the map is most likely to have: a plausible-looking
        name like "Order Rejected" that no engine logs. The mapping looks complete,
        the tests pass, and the operator is never notified.
        """
        emitted: set[str] = set()
        for path in _ARC.rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and node.func.id == "log_event"
                    and len(node.args) >= 2
                ):
                    # Every string literal in the label position, not just a bare
                    # Constant: several sites choose between two labels inline
                    # (`"Order Filled" if complete else "Partial Fill"`), and those
                    # are exactly the labels a naive walk would report as missing.
                    emitted.update(
                        sub.value
                        for sub in ast.walk(node.args[1])
                        if isinstance(sub, ast.Constant) and isinstance(sub.value, str)
                    )
        missing = sorted(set(EVENT_CATEGORY) - emitted)
        assert not missing, f"mapped but never logged anywhere in arc/: {missing}"

    def test_every_category_except_the_summary_has_a_source(self) -> None:
        """Daily Summary is sent directly; the other twenty come from events."""
        reachable = set(EVENT_CATEGORY.values()) | {"fatal_errors", "warnings", "daily_summary"}
        assert set(CATEGORIES) - reachable == set()


class TestSeverityFallback:
    def test_unmapped_error_still_reaches_the_operator(self) -> None:
        assert category_for("Something New", "ERROR") == "fatal_errors"
        assert category_for("Something New", "WARNING") == "warnings"

    def test_unmapped_info_is_dropped(self) -> None:
        """Not every INFO line is worth a phone notification."""
        assert category_for("Something New", "INFO") is None

    def test_explicit_mapping_wins_over_severity(self) -> None:
        assert category_for("Order Filled", "WARNING") == "order_filled"


class TestRendering:
    def test_disabled_category_renders_nothing(self) -> None:
        assert _notifier(order_filled=False).render(_signal("Order Filled")) is None

    def test_enabled_category_renders_label_event_and_detail(self) -> None:
        text = _notifier().render(_signal("Order Filled"))
        assert text is not None
        assert "Order Filled" in text
        assert text.endswith("d")

    def test_status_frames_are_not_notifications(self) -> None:
        """The hub carries status frames at 5Hz. Forwarding them would be a flood."""
        assert _notifier().render({"type": "status", "data": {"event": "x"}}) is None

    def test_own_events_are_never_forwarded(self) -> None:
        """A send failure logs a warning; forwarding it would notify about itself.

        Without this, one Telegram outage produces an unbounded loop of warnings
        each of which attempts another send.
        """
        assert _notifier().render(_signal("Telegram Send Failed", "WARNING", "Telegram")) is None


class TestUnconfiguredIsInert:
    def test_no_token_means_not_configured(self) -> None:
        assert TelegramNotifier(token="", chat_id="c", flags={}).configured is False
        assert TelegramNotifier(token="t", chat_id="", flags={}).configured is False

    def test_send_returns_false_without_network(self) -> None:
        notifier = TelegramNotifier(token="", chat_id="", flags={})
        assert asyncio.run(notifier.send("x")) is False
        assert notifier.sent == 0

    def test_run_returns_immediately_and_leaves_no_subscriber(self) -> None:
        """An unconfigured notifier must not hold a hub queue open forever."""
        hub = EventHub()

        async def go() -> None:
            await asyncio.wait_for(TelegramNotifier(token="", chat_id="", flags={}).run(hub), 1.0)

        asyncio.run(go())
        assert hub.subscriber_count == 0


class TestHubIntegration:
    def test_a_real_log_line_becomes_a_notification(self) -> None:
        """End to end over the actual logging path, not a hand-built dict."""
        hub = EventHub()
        logger = logging.getLogger("arc.test_telegram")
        logger.handlers.clear()
        logger.setLevel(logging.DEBUG)
        logger.propagate = False
        attach(hub, logger)

        sent: list[str] = []
        notifier = _notifier()

        async def go() -> None:
            queue = hub.subscribe()
            hub.bind_loop(asyncio.get_running_loop())
            log_event(logging.INFO, "PTB Frozen", "btc 120153.42", logger=logger)
            text = notifier.render(await asyncio.wait_for(queue.get(), 1.0))
            if text is not None:
                sent.append(text)
            hub.unsubscribe(queue)

        asyncio.run(go())
        assert sent and "PTB Frozen" in sent[0]

    def test_subscriber_released_on_cancel(self) -> None:
        hub = EventHub()
        notifier = _notifier()

        async def go() -> None:
            hub.bind_loop(asyncio.get_running_loop())
            task = asyncio.create_task(notifier.run(hub))
            await asyncio.sleep(0)
            assert hub.subscriber_count == 1
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

        asyncio.run(go())
        assert hub.subscriber_count == 0


class TestDailySummary:
    def test_first_call_arms_rather_than_fires(self) -> None:
        """Otherwise every restart sends a summary of a run one tick old."""
        notifier = _notifier()
        assert notifier.summary_due(1_000_000.0) is False

    def test_fires_once_a_day(self) -> None:
        notifier = _notifier()
        notifier.summary_due(0.0)
        assert notifier.summary_due(86_399.0) is False
        assert notifier.summary_due(86_400.0) is True
        assert notifier.summary_due(86_400.0) is False

    def test_disabled_category_never_fires(self) -> None:
        notifier = _notifier(daily_summary=False)
        notifier.summary_due(0.0)
        assert notifier.summary_due(200_000.0) is False


class TestNotificationOnly:
    def test_no_inbound_telegram_api_is_used(self) -> None:
        """sendMessage and nothing else.

        getUpdates, setWebhook or answerCallbackQuery would each give the chat a
        path INTO the runtime, making the Telegram account a second set of trading
        credentials held by whichever phone is signed in.
        """
        source = (_ARC / "notify" / "telegram.py").read_text(encoding="utf-8")
        for method in ("getUpdates", "setWebhook", "deleteWebhook", "answerCallbackQuery"):
            assert method not in source

    def test_the_notifier_cannot_reach_the_runtime(self) -> None:
        """No import of any engine, store, or gate. It only reads the event hub."""
        tree = ast.parse((_ARC / "notify" / "telegram.py").read_text(encoding="utf-8"))
        imported = {
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module
        }
        forbidden = {
            m
            for m in imported
            if m.startswith("arc.")
            and m not in {"arc.logging_setup", "arc.runtime.events"}
        }
        assert not forbidden, f"notifier reaches into {sorted(forbidden)}"
