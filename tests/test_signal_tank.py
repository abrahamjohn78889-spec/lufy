"""Signal Tank: one bounded stream over the existing log, no duplicates.

The failures pinned here are the ones that would show up only in production: a
buffer that grows for days, a duplicated handler that reads as duplicated trading
activity, and a slow browser back-pressuring the engine.
"""

from __future__ import annotations

import asyncio
import logging

from arc.logging_setup import log_event
from arc.runtime.events import MAX_EVENTS, EventHub, SignalTankHandler, attach


def _logger(hub: EventHub) -> logging.Logger:
    logger = logging.getLogger("arc.test_signal_tank")
    logger.handlers.clear()
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    attach(hub, logger)
    return logger


class TestEveryLogLineBecomesAnEvent:
    def test_log_event_reaches_the_tank(self) -> None:
        hub = EventHub()
        log_event(logging.INFO, "PTB Frozen", "btc-up-or-down 120153.42", logger=_logger(hub))
        (event,) = hub.recent()
        assert event.event == "PTB Frozen"
        assert event.detail == "btc-up-or-down 120153.42"
        assert event.severity == "INFO"
        assert event.engine

    def test_severity_maps_fatal(self) -> None:
        hub = EventHub()
        log_event(logging.CRITICAL, "Fatal Error", "feed gone", logger=_logger(hub))
        assert hub.recent()[0].severity == "FATAL"

    def test_sequence_never_repeats(self) -> None:
        hub = EventHub()
        logger = _logger(hub)
        for i in range(5):
            log_event(logging.INFO, f"E{i}", logger=logger)
        assert [e.seq for e in hub.recent()] == [1, 2, 3, 4, 5]


class TestNoUnboundedGrowth:
    def test_buffer_is_capped(self) -> None:
        """A 24x7 process must not accumulate events for days."""
        hub = EventHub()
        for i in range(MAX_EVENTS + 50):
            hub.emit("Runtime", "INFO", f"E{i}", "", 1.0)
        assert len(hub.recent(limit=0)) == MAX_EVENTS
        # The newest survive, the oldest are dropped.
        assert hub.recent(limit=1)[0].event == f"E{MAX_EVENTS + 49}"


class TestNoDuplicateEvents:
    def test_attach_is_idempotent(self) -> None:
        """Two handlers would double every line and read as double trading."""
        hub = EventHub()
        logger = _logger(hub)
        attach(hub, logger)
        assert sum(isinstance(h, SignalTankHandler) for h in logger.handlers) == 1
        log_event(logging.INFO, "Once", logger=logger)
        assert len(hub.recent()) == 1


class TestSubscribersNeverBlockTheEngine:
    def test_slow_subscriber_drops_oldest_and_keeps_newest(self) -> None:
        async def scenario() -> None:
            hub = EventHub()
            hub.bind_loop(asyncio.get_running_loop())
            queue = hub.subscribe()
            for i in range(2000):
                hub.emit("Runtime", "INFO", f"E{i}", "", 1.0)
            await asyncio.sleep(0)
            assert not queue.full() or queue.qsize() <= 500
            drained = [queue.get_nowait()["data"]["event"] for _ in range(queue.qsize())]
            assert drained[-1] == "E1999"
            hub.unsubscribe(queue)
            assert hub.subscriber_count == 0

        asyncio.run(scenario())

    def test_unsubscribe_removes_the_queue(self) -> None:
        async def scenario() -> None:
            hub = EventHub()
            queue = hub.subscribe()
            hub.unsubscribe(queue)
            hub.unsubscribe(queue)  # idempotent: a double close must not raise
            assert hub.subscriber_count == 0

        asyncio.run(scenario())
