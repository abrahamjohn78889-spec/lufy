"""Signal Tank: the live runtime stream, and the fan-out the WebSocket reads.

Signal Tank is not a second event system. ARC already emits one plain line for
every significant action through `log_event`, so this attaches a logging handler
to that same stream instead of asking forty call sites to publish twice. A parallel
publish path would drift: the day someone adds a log line and forgets the publish,
the dashboard goes quiet while the log file shows the incident.

The buffer is bounded. A 24x7 process that appended events forever would grow
without limit and the leak would only show up after days of uptime, which is
exactly when nobody is watching.

Nothing here is in the trading path. A subscriber that raises, or a queue that
fills, drops events for that subscriber alone and never blocks the engine — a
dashboard that stalls must not be able to stall order submission.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections import deque
from dataclasses import dataclass
from typing import Any, Final

from arc.logging_setup import LOGGER_NAME
from arc.timefmt import stamps

__all__ = ["MAX_EVENTS", "EventHub", "SignalEvent", "SignalTankHandler", "attach"]

# Roughly a day of ordinary activity at ARC's event rate, and a hard ceiling on the
# memory this can ever hold.
MAX_EVENTS: Final[int] = 5_000

# Per-subscriber queue depth. A browser that stops reading loses its oldest events
# rather than pushing back on the runtime.
_QUEUE_DEPTH: Final[int] = 500

_SEVERITY: Final[dict[int, str]] = {
    logging.DEBUG: "DEBUG",
    logging.INFO: "INFO",
    logging.WARNING: "WARNING",
    logging.ERROR: "ERROR",
    logging.CRITICAL: "FATAL",
}


@dataclass(frozen=True, slots=True)
class SignalEvent:
    """One line of the runtime console. Timestamp, engine, severity, message."""

    seq: int
    ts: float
    engine: str
    severity: str
    event: str
    detail: str
    runtime_session_id: str = ""

    def as_json(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "ts": self.ts,
            # Both zones travel with the event rather than being derived in the
            # browser. One conversion, in one place (arc.timefmt), so an event in
            # the Signal Tank, the same event in the Ledger and the same event in
            # Telegram cannot render three different wall clocks.
            **{k: v for k, v in stamps(self.ts).items() if k != "utc"},
            "engine": self.engine,
            "severity": self.severity,
            "event": self.event,
            "detail": self.detail,
            "runtime_session_id": self.runtime_session_id,
        }


def _engine_of(record: logging.LogRecord) -> str:
    """Which engine emitted this, derived from the module that logged it.

    Taken from the record rather than passed in at every call site: a source label
    that has to be supplied by hand is a label that will be wrong somewhere, and a
    wrong engine name sends the operator to the wrong panel.
    """
    module = record.module
    return {
        "engine": "Runtime",
        "rotation": "Market Engine",
        "discovery": "Market Engine",
        "feed": "Provider",
        "providers": "Provider",
        "settlement_feed": "Provider",
        "watchdog": "Provider",
        "ptb": "Market Engine",
        "spec_check": "Market Engine",
        "validation": "Market Engine",
        "activation": "Window Engine",
        "freeze": "Window Engine",
        "evaluate": "Window Engine",
        "lifecycle": "Window Engine",
        "submit": "Limit Order Engine",
        "reprice": "Limit Order Engine",
        "sweep": "Limit Order Engine",
        "fill_engine": "Limit Order Engine",
        "reconcile": "Recovery Engine",
        "recovery": "Recovery Engine",
        "state": "Runtime",
        "telegram": "Telegram",
    }.get(module, module.replace("_", " ").title())


class EventHub:
    """The one buffer, plus every live WebSocket subscriber.

    Owns no event loop of its own. Publishing happens from whichever context logged
    the line — including a thread with no running loop — so delivery to async
    subscribers goes through `call_soon_threadsafe` when a loop is known and is
    otherwise a plain queue put.
    """

    __slots__ = (
        "_errors",
        "_events",
        "_loop",
        "_seq",
        "_subscribers",
        "_warnings",
        "runtime_session_id",
    )

    def __init__(self) -> None:
        self._events: deque[SignalEvent] = deque(maxlen=MAX_EVENTS)
        self._subscribers: list[asyncio.Queue[dict[str, Any]]] = []
        self._seq = 0
        self._loop: asyncio.AbstractEventLoop | None = None
        self.runtime_session_id = ""
        # Counted at emit, not by scanning `_events`: that deque is bounded at
        # MAX_EVENTS, so a long run would silently under-report every warning that
        # had already aged out of the buffer.
        self._warnings = 0
        self._errors = 0

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    # ── reading ──────────────────────────────────────────────────────────────

    def recent(self, limit: int = 200) -> tuple[SignalEvent, ...]:
        """The newest events, oldest first.

        A reconnecting dashboard replays these so the console is populated rather
        than blank: an empty Signal Tank after a socket blip reads as "nothing is
        happening", which is the one thing it must never say incorrectly.
        """
        events = tuple(self._events)
        return events[-limit:] if limit > 0 else events

    @property
    def sequence(self) -> int:
        return self._seq

    @property
    def warning_count(self) -> int:
        """Monotonic count of WARNING lines since this hub was created."""
        return self._warnings

    @property
    def error_count(self) -> int:
        """Monotonic count of ERROR and CRITICAL lines since this hub was created."""
        return self._errors

    # ── writing ──────────────────────────────────────────────────────────────

    def publish_event(self, event: SignalEvent) -> None:
        self._events.append(event)
        self.broadcast({"type": "signal", "data": event.as_json()})

    def emit(
        self,
        engine: str,
        severity: str,
        event: str,
        detail: str,
        ts: float,
        runtime_session_id: str = "",
    ) -> SignalEvent:
        self._seq += 1
        if severity == "WARNING":
            self._warnings += 1
        elif severity in ("ERROR", "CRITICAL"):
            self._errors += 1
        signal = SignalEvent(
            seq=self._seq, ts=ts, engine=engine, severity=severity, event=event, detail=detail,
            runtime_session_id=runtime_session_id or self.runtime_session_id,
        )
        self.publish_event(signal)
        return signal

    def broadcast(self, message: dict[str, Any]) -> None:
        """Push one message to every subscriber. Never raises, never blocks."""
        loop = self._loop
        for queue in list(self._subscribers):
            if loop is not None and loop.is_running():
                try:
                    loop.call_soon_threadsafe(self._offer, queue, message)
                except RuntimeError:
                    # The loop closed between the check and the call. The subscriber
                    # is already gone; dropping is correct and must not propagate
                    # into the caller, which may be the engine.
                    continue
            else:
                self._offer(queue, message)

    @staticmethod
    def _offer(queue: asyncio.Queue[dict[str, Any]], message: dict[str, Any]) -> None:
        if queue.full():
            # Drop the OLDEST, not the newest. A slow reader should fall behind, not
            # miss the event that just happened.
            with contextlib.suppress(asyncio.QueueEmpty):
                queue.get_nowait()
        with contextlib.suppress(asyncio.QueueFull):
            queue.put_nowait(message)

    # ── subscriptions ────────────────────────────────────────────────────────

    def subscribe(self) -> asyncio.Queue[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=_QUEUE_DEPTH)
        self._subscribers.append(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[dict[str, Any]]) -> None:
        """Removal must be unconditional: a queue left behind is a permanent leak."""
        if queue in self._subscribers:
            self._subscribers.remove(queue)

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)


class SignalTankHandler(logging.Handler):
    """Turns every ARC log line into a Signal Tank event.

    Installed on the `arc` logger, after the redaction filter, so a secret that
    reached a log message is masked before it reaches a browser.
    """

    def __init__(self, hub: EventHub) -> None:
        super().__init__(level=logging.DEBUG)
        self._hub = hub

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._hub.emit(
                engine=_engine_of(record),
                severity=_SEVERITY.get(record.levelno, "INFO"),
                event=record.getMessage(),
                detail=str(getattr(record, "arc_detail", "")),
                ts=record.created,
                runtime_session_id=str(getattr(record, "arc_session_id", "")),
            )
        except Exception:  # pragma: no cover - handleError is the logging contract
            self.handleError(record)


def attach(hub: EventHub, logger: logging.Logger | None = None) -> SignalTankHandler:
    """Wire the hub to the ARC logger. Idempotent.

    Re-attaching would duplicate every Signal Tank entry, and duplicated events read
    as duplicated trading activity.
    """
    target = logger if logger is not None else logging.getLogger(LOGGER_NAME)
    for existing in target.handlers:
        if isinstance(existing, SignalTankHandler):
            return existing
    handler = SignalTankHandler(hub)
    target.addHandler(handler)
    return handler
