"""The `/ws` endpoint. One socket carries every runtime update.

The dashboard never polls. A browser polling ten panels at one second each is ten
requests per second forever on a 24x7 process, and each one renders a slightly
different instant — the panels disagree with each other about which market is open.
One push of one document per tick means every panel is drawn from the same state.

Two message kinds go out: `status` (the whole document, on a timer) and `signal`
(one Signal Tank line, the moment it is logged). Events are pushed rather than
folded into the next status frame because the operator's console has to be live —
an event that waits for the next tick is an event that arrives after the fill it
was describing.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import TYPE_CHECKING, Any

from fastapi import WebSocket, WebSocketDisconnect

from arc.api.models import status_payload

if TYPE_CHECKING:  # pragma: no cover - annotations only
    from arc.runtime.engine import ArcRuntime

__all__ = ["STATUS_INTERVAL", "serve"]

# Faster than this pushes the whole document more often than a human can read it;
# slower makes the countdown visibly step. The browser interpolates the countdown
# from close_ts between frames, so this only sets how often values refresh.
STATUS_INTERVAL: float = 1.0

_REPLAY = 200


async def _push_status(socket: WebSocket, run: ArcRuntime) -> None:
    while True:
        payload = await status_payload(run, run.clock.now())
        await socket.send_json({"type": "status", "data": payload})
        await asyncio.sleep(STATUS_INTERVAL)


async def _push_events(
    socket: WebSocket, queue: asyncio.Queue[dict[str, Any]], replayed_through: int
) -> None:
    while True:
        message = await queue.get()
        seq = message.get("data", {}).get("seq")
        if message.get("type") == "signal" and isinstance(seq, int) and seq <= replayed_through:
            # Already sent by the replay. Subscribing before reading the backlog is
            # what makes the handover lossless, and the cost of that ordering is this
            # overlap — without the floor the operator sees the same event twice and
            # reads it as two fills.
            continue
        await socket.send_json(message)


async def serve(socket: WebSocket, run: ArcRuntime) -> None:
    """Accept one dashboard connection and stream until it goes away.

    The backlog is replayed on connect. A reconnecting operator with an empty
    console cannot tell a quiet runtime from a broken one, and the reconnect is
    exactly when they are trying to find out which it was.
    """
    await socket.accept()
    # Subscribe BEFORE reading the backlog: the other order drops any event published
    # in the gap, and the gap is exactly when the runtime is busiest.
    queue = run.hub.subscribe()
    replayed_through = 0
    for event in run.hub.recent(_REPLAY):
        replayed_through = max(replayed_through, event.seq)
        await socket.send_json({"type": "signal", "data": event.as_json()})

    tasks = [
        asyncio.create_task(_push_status(socket, run)),
        asyncio.create_task(_push_events(socket, queue, replayed_through)),
    ]
    try:
        # First task to finish ends the connection: if the status pusher dies the
        # dashboard would keep showing the last frame as though it were live, which
        # is the one thing the stale-data rule forbids.
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    except WebSocketDisconnect:
        pass
    finally:
        run.hub.unsubscribe(queue)
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
