"""The RTDS websocket: the four properties a conventional client gets wrong.

Every one of these is tested against an injected connector rather than a socket,
because the failures they guard against are silent. A subscribe filter produces an
open connection that delivers nothing; a JSON keepalive produces a connection the
relay closes on its own timer. Both look like a quiet market, so a test that only
checked "did we connect" would pass in exactly the cases that matter.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator
from typing import Any, cast

import pytest
from conftest import WINDOW_TS

from arc.clock import FrozenClock
from arc.errors import FeedError
from arc.market.feed import (
    CHAINLINK_TOPIC,
    KEEPALIVE_FRAME,
    RTDS_URL,
    SDK_STALE_THRESHOLD_MS,
    BackoffPolicy,
    BoundaryTracker,
    RtdsFeed,
    subscribe_frame,
)


class _Socket:
    """A scripted websocket. Yields frames, then closes or raises."""

    def __init__(self, *frames: str | bytes, fail_with: Exception | None = None) -> None:
        self._frames = list(frames)
        self._fail_with = fail_with
        self.sent: list[str] = []
        self.closed = False

    async def send(self, data: str) -> None:
        self.sent.append(data)

    async def close(self) -> None:
        self.closed = True

    def __aiter__(self) -> _Socket:
        return self

    async def __anext__(self) -> str | bytes:
        if self._frames:
            return self._frames.pop(0)
        if self._fail_with is not None:
            raise self._fail_with
        raise StopAsyncIteration


def _feed(*sockets: object, **kwargs: Any) -> tuple[RtdsFeed, list[object]]:
    """A feed whose connector replays `sockets` in order. Exceptions are raised."""
    pending = list(sockets)
    handed: list[object] = []

    async def connect(url: str) -> Any:
        item = pending.pop(0) if pending else _Socket()
        if isinstance(item, Exception):
            raise item
        handed.append(item)
        return item

    kwargs.setdefault("backoff", BackoffPolicy(initial_seconds=0.001, max_seconds=0.002))
    kwargs.setdefault("keepalive_seconds", 3600.0)
    feed = RtdsFeed(FrozenClock(now=float(WINDOW_TS)), connect=connect, **kwargs)
    return feed, handed


def _stream(feed: RtdsFeed) -> AsyncGenerator[str | bytes, None]:
    """`messages()` is annotated AsyncIterator, which is the right contract for callers.

    These tests additionally need `aclose()` to shut the socket down deterministically —
    without it the pending reconnect task outlives the test and the "socket closed"
    assertions race. Casting here keeps that a property of the test harness rather than
    widening the public signature.
    """
    return cast(AsyncGenerator[str | bytes, None], feed.messages())


async def _take(feed: RtdsFeed, count: int) -> list[str | bytes]:
    """Read `count` frames, then stop. The stream itself never ends."""
    out: list[str | bytes] = []
    stream = _stream(feed)
    async for frame in stream:
        out.append(frame)
        if len(out) >= count:
            break
    await stream.aclose()
    return out


class TestProtocolProperties:
    def test_the_url_is_the_live_data_relay(self) -> None:
        assert RTDS_URL == "wss://ws-live-data.polymarket.com"

    def test_the_subscribe_frame_carries_the_topic_and_nothing_else(self) -> None:
        """Property 1: a filter list produces a subscription that delivers nothing on
        an otherwise healthy connection, which reads as a quiet market."""
        frame = subscribe_frame()
        assert frame == {"action": "subscribe", "subscriptions": [{"topic": CHAINLINK_TOPIC}]}
        assert frame["subscriptions"] == [{"topic": CHAINLINK_TOPIC}]

    def test_the_subscription_object_has_no_filter_keys(self) -> None:
        subscription = subscribe_frame()["subscriptions"][0]
        for forbidden in ("filters", "symbols", "assets", "feedIds", "feed_ids", "pairs"):
            assert forbidden not in subscription

    def test_the_topic_is_the_chainlink_price_topic(self) -> None:
        assert CHAINLINK_TOPIC == "crypto_prices_chainlink"

    def test_the_keepalive_is_the_literal_string_not_json(self) -> None:
        """Property 2: {"type":"ping"} is discarded and the relay closes on its timer."""
        assert KEEPALIVE_FRAME == "PING"
        with pytest.raises(json.JSONDecodeError):
            json.loads(KEEPALIVE_FRAME)

    def test_the_sdk_stale_threshold_is_ten_minutes(self) -> None:
        """Property 4: the relay's own notion of a dead subscription. Not ARC's policy."""
        assert SDK_STALE_THRESHOLD_MS == 600_000

    def test_the_subscribe_frame_is_sent_as_json_on_connect(self) -> None:
        socket = _Socket("tick")
        feed, _ = _feed(socket)
        asyncio.run(_take(feed, 1))
        assert json.loads(socket.sent[0]) == subscribe_frame()

    def test_no_snapshot_is_awaited_before_the_feed_is_considered_up(self) -> None:
        """Property 3: the first message arrives with the next tick. Code that waits
        for an initial state waits forever."""
        socket = _Socket(fail_with=StopAsyncIteration())
        feed, _ = _feed(socket, _Socket("late-tick"))

        async def scenario() -> list[str | bytes]:
            return await _take(feed, 1)

        frames = asyncio.run(scenario())
        # The first socket delivered nothing and the feed still reported connected
        # before any frame arrived; it reconnected rather than blocking on a snapshot.
        assert frames == ["late-tick"]
        assert feed.connect_attempts == 2

    def test_the_keepalive_is_sent_on_the_timer(self) -> None:
        socket = _Socket("a", "b", "c")
        feed, _ = _feed(socket, keepalive_seconds=0.001)

        async def scenario() -> None:
            stream = _stream(feed)
            async for _ in stream:
                await asyncio.sleep(0.005)
                break
            await asyncio.sleep(0.005)
            await stream.aclose()

        asyncio.run(scenario())
        assert feed.keepalives_sent >= 1
        assert all(sent == KEEPALIVE_FRAME for sent in socket.sent[1:])


class TestEchoedKeepalives:
    def test_echoed_ping_and_pong_frames_are_swallowed(self) -> None:
        """A caller must not have to know the keepalive exists to parse the stream."""
        feed, _ = _feed(_Socket("PING", "pong", "  PONG  ", "real-tick"))
        assert asyncio.run(_take(feed, 1)) == ["real-tick"]

    def test_swallowed_frames_still_count_as_received(self) -> None:
        feed, _ = _feed(_Socket("PING", "real-tick"))
        asyncio.run(_take(feed, 1))
        assert feed.messages_received == 2


class TestBackoff:
    def test_the_delay_grows_geometrically(self) -> None:
        policy = BackoffPolicy(initial_seconds=0.5, max_seconds=30.0, multiplier=2.0)
        assert policy.delay_for(1) == 0.5
        assert policy.delay_for(2) == 1.0
        assert policy.delay_for(3) == 2.0

    def test_the_delay_is_capped(self) -> None:
        """Unbounded backoff means minutes of blindness after the feed came back."""
        policy = BackoffPolicy(initial_seconds=0.5, max_seconds=30.0, multiplier=2.0)
        assert policy.delay_for(50) == 30.0

    def test_attempts_are_one_based(self) -> None:
        with pytest.raises(ValueError, match="1-based"):
            BackoffPolicy().delay_for(0)

    def test_a_non_positive_initial_delay_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="initial_seconds"):
            BackoffPolicy(initial_seconds=0.0)

    def test_a_max_below_the_initial_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="max_seconds"):
            BackoffPolicy(initial_seconds=5.0, max_seconds=1.0)

    def test_a_multiplier_of_one_would_never_back_off(self) -> None:
        with pytest.raises(ValueError, match="multiplier"):
            BackoffPolicy(multiplier=1.0)


class TestReconnection:
    def test_the_stream_continues_across_a_dropped_connection(self) -> None:
        """Only the caller's cancellation ends the loop; a drop is reconnected."""
        feed, _ = _feed(_Socket("first"), _Socket("second"))
        assert asyncio.run(_take(feed, 2)) == ["first", "second"]
        assert feed.connect_attempts == 2

    def test_a_connect_failure_is_retried_rather_than_raised(self) -> None:
        feed, _ = _feed(OSError("refused"), _Socket("after-retry"))
        assert asyncio.run(_take(feed, 1)) == ["after-retry"]
        assert feed.connect_attempts == 2

    def test_the_attempt_counter_resets_after_a_successful_connect(self) -> None:
        """Otherwise one bad night leaves the backoff pinned at its cap forever."""
        feed, _ = _feed(OSError("refused"), _Socket("ok"), _Socket("ok-again"))
        asyncio.run(_take(feed, 2))
        assert feed.connect_attempts == 3

    def test_the_socket_is_closed_when_the_caller_stops_reading(self) -> None:
        socket = _Socket("tick", "tick2")
        feed, _ = _feed(socket)
        asyncio.run(_take(feed, 1))
        assert socket.closed is True


class TestBoundaryContinuity:
    def test_a_fresh_tracker_vouches_for_nothing(self) -> None:
        """It has observed no boundary, so it can vouch for none."""
        assert BoundaryTracker().continuous is False

    def test_connecting_does_not_by_itself_restore_continuity(self) -> None:
        tracker = BoundaryTracker()
        tracker.mark_connected()
        assert tracker.continuous is False

    def test_a_boundary_crossed_while_connected_is_continuous(self) -> None:
        tracker = BoundaryTracker()
        tracker.mark_connected()
        assert tracker.observe_boundary() is True
        assert tracker.continuous is True

    def test_a_drop_clears_continuity_immediately(self) -> None:
        """This flag is the gate on the L2 PTB source (ptb.py)."""
        tracker = BoundaryTracker()
        tracker.mark_connected()
        tracker.observe_boundary()
        assert tracker.continuous is True
        tracker.mark_disconnected()
        assert tracker.continuous is False

    def test_a_boundary_the_reconnect_preceded_is_continuous(self) -> None:
        """The gap ended before the boundary, so the connection did span it.

        Continuity is a claim about the interval containing the boundary, not about
        the whole session. A boundary that falls INSIDE the gap produces no
        observation at all, so observe_boundary is never called for it and ptb.py
        refuses the stale reference on boundary_ts instead.
        """
        tracker = BoundaryTracker()
        tracker.mark_connected()
        tracker.mark_disconnected()
        tracker.mark_connected()
        assert tracker.observe_boundary() is True

    def test_continuity_is_not_claimed_between_the_drop_and_the_reconnect(self) -> None:
        """Nothing observed in the gap can be vouched for."""
        tracker = BoundaryTracker()
        tracker.mark_connected()
        tracker.observe_boundary()
        tracker.mark_disconnected()
        assert tracker.observe_boundary() is False

    def test_the_feed_marks_the_boundary_broken_on_a_disconnect(self) -> None:
        feed, _ = _feed(_Socket("first"), _Socket("second"))

        async def scenario() -> None:
            stream = _stream(feed)
            async for _ in stream:
                pass_through = feed.boundary
                pass_through.mark_connected()
                pass_through.observe_boundary()
                break
            # Force the drop by exhausting the first socket.
            async for _ in stream:
                break
            await stream.aclose()

        asyncio.run(scenario())
        assert feed.boundary.continuous is False

    def test_the_feed_marks_the_boundary_connected_on_connect(self) -> None:
        feed, _ = _feed(_Socket("tick"))
        asyncio.run(_take(feed, 1))
        # mark_connected clears continuity: a new connection has crossed no boundary.
        assert feed.boundary.continuous is False
        assert feed.boundary.observe_boundary() is True


class TestConnectFailures:
    def test_a_websocket_error_on_connect_becomes_a_feed_error(self) -> None:
        """Operational, not fatal: the process keeps its dashboard and its data (A8)."""

        async def connect_fails(url: str) -> Any:
            raise OSError("no route to host")

        feed_direct = RtdsFeed(
            FrozenClock(now=1.0),
            connect=connect_fails,
            backoff=BackoffPolicy(initial_seconds=0.001, max_seconds=0.002),
        )
        with pytest.raises(FeedError, match="failed"):
            asyncio.run(feed_direct._open())

    def test_the_configured_url_is_reported(self) -> None:
        feed, _ = _feed(url="wss://example.invalid")
        assert feed.url == "wss://example.invalid"
