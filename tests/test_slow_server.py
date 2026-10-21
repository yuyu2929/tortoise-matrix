"""The headline tests: a slow server must not slow the client down."""
from __future__ import annotations

import asyncio
import time

from conftest import ROOM


async def wait_until(predicate, timeout=10.0, interval=0.1):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if await predicate():
            return True
        await asyncio.sleep(interval)
    return False


async def test_send_returns_instantly_on_slow_server(slow_client):
    t0 = time.perf_counter()
    ev = await slow_client.send_message(ROOM, "@me:localhost", "fast!")
    elapsed_ms = (time.perf_counter() - t0) * 1000
    assert ev.status == "pending"
    # The server takes ~2000ms; the UI must see the message in <100ms.
    assert elapsed_ms < 100, f"send blocked for {elapsed_ms:.1f}ms"


async def test_reads_stay_fast_while_server_is_slow(slow_client):
    t0 = time.perf_counter()
    for _ in range(50):
        await slow_client.get_messages(ROOM)
    elapsed_ms = (time.perf_counter() - t0) * 1000
    assert elapsed_ms < 500, f"50 reads took {elapsed_ms:.1f}ms"


async def test_message_eventually_acknowledged(slow_client):
    ev = await slow_client.send_message(ROOM, "@me:localhost", "ack me")
    assert ev.local_echo_id

    async def acked():
        msgs = await slow_client.get_messages(ROOM)
        return any(m["event_id"] for m in msgs if m["local_echo_id"] == ev.local_echo_id)

    assert await wait_until(acked, timeout=15.0)
    msgs = await slow_client.get_messages(ROOM)
    row = next(m for m in msgs if m["local_echo_id"] == ev.local_echo_id)
    assert row["status"] == "sent"
    assert row["event_id"].startswith("$")


async def test_optimistic_visible_before_ack(slow_client):
    """The message must be locally visible long before the server answers."""
    ev = await slow_client.send_message(ROOM, "@me:localhost", "instant")
    msgs = await slow_client.get_messages(ROOM)  # immediate, no waiting
    assert any(m["local_echo_id"] == ev.local_echo_id for m in msgs)


async def test_slow_server_does_not_block_room_ops(slow_client):
    """While a send is in flight, other operations keep working."""
    await slow_client.send_message(ROOM, "@me:localhost", "in flight")
    t0 = time.perf_counter()
    rooms = await slow_client.get_rooms()
    elapsed_ms = (time.perf_counter() - t0) * 1000
    assert rooms and elapsed_ms < 50


async def test_retry_survives_packet_loss(slow_client):
    """With the server refusing everything, messages stay queued, not lost."""
    slow_client.transport.failure_rate = 1.0
    ev = await slow_client.send_message(ROOM, "@me:localhost", "will retry")

    async def retried_at_least_once():
        row = next(
            m for m in await slow_client.get_messages(ROOM)
            if m["local_echo_id"] == ev.local_echo_id
        )
        return row["retry_count"] >= 1

    # The first attempt may take up to ~3.5s (latency+jitter) to even fail;
    # poll until the queue has visibly fought back.
    assert await wait_until(retried_at_least_once, timeout=20.0)
    row = next(
        m for m in await slow_client.get_messages(ROOM)
        if m["local_echo_id"] == ev.local_echo_id
    )
    assert row["event_id"] is None  # nothing was faked

    # Now let the server recover: the message must land without user action.
    slow_client.transport.failure_rate = 0.0

    async def acked():
        m = next(
            m for m in await slow_client.get_messages(ROOM)
            if m["local_echo_id"] == ev.local_echo_id
        )
        return m["event_id"] is not None

    assert await wait_until(acked, timeout=30.0, interval=0.5)


async def test_permanent_rejection_marks_message(slow_client):
    """A message to a nonexistent room is rejected, retried forever in vain."""
    ev = await slow_client.send_message("!ghost:localhost", "@me:localhost", "nope")

    async def rejected():
        msgs = await slow_client.get_messages("!ghost:localhost")
        return any(m["status"] == "rejected" for m in msgs)

    # With infinite retry, rejection comes from the first PermanentError.
    assert await wait_until(rejected, timeout=10.0)
