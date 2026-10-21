"""Optimistic engine tests: the local view is always instant and truthful."""
from __future__ import annotations

import asyncio

from conftest import ROOM


async def wait_until(predicate, timeout=10.0, interval=0.05):
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if await predicate():
            return True
        await asyncio.sleep(interval)
    return False


async def test_edit_is_optimistic(fast_client):
    ev = await fast_client.send_message(ROOM, "@me:localhost", "v1")
    edited = await fast_client.edit_message(ROOM, ev.local_echo_id, "v2")
    assert edited.body == "v2" and edited.edited
    msgs = await fast_client.get_messages(ROOM)
    row = next(m for m in msgs if m["local_echo_id"] == ev.local_echo_id)
    assert row["body"] == "v2"


async def test_delete_is_optimistic(fast_client):
    ev = await fast_client.send_message(ROOM, "@me:localhost", "delete me")
    await fast_client.delete_message(ROOM, ev.local_echo_id)
    msgs = await fast_client.get_messages(ROOM)
    row = next(m for m in msgs if m["local_echo_id"] == ev.local_echo_id)
    assert row["redacted"] and row["body"] == ""


async def test_edit_after_ack_reaches_server(fast_client):
    ev = await fast_client.send_message(ROOM, "@me:localhost", "v1")

    async def acked():
        m = next(
            m for m in await fast_client.get_messages(ROOM)
            if m["local_echo_id"] == ev.local_echo_id
        )
        return m["event_id"] is not None

    assert await wait_until(acked, timeout=5.0)
    event_id = next(
        m["event_id"] for m in await fast_client.get_messages(ROOM)
        if m["local_echo_id"] == ev.local_echo_id
    )
    await fast_client.edit_message(ROOM, event_id, "v2")

    async def server_edited():
        events = fast_client.transport.events
        return any(
            e["type"] == "m.room.message.edit" and e["target_event_id"] == event_id
            for e in events
        )

    assert await wait_until(server_edited, timeout=5.0)


async def test_delete_before_ack_skips_delivery(fast_client):
    """Deleting a still-pending message means it is never even sent."""
    transport = fast_client.transport
    calls_before = len([e for e in transport.events if e["type"] == "m.room.message"])
    ev = await fast_client.send_message(ROOM, "@me:localhost", "never mind")
    await fast_client.delete_message(ROOM, ev.local_echo_id)
    await asyncio.sleep(0.5)
    calls_after = len([e for e in transport.events if e["type"] == "m.room.message"])
    assert calls_after == calls_before  # nothing was delivered


async def test_event_stream_publishes_upsert_and_ack(fast_client):
    received = []
    handler = lambda ev: received.append(ev)  # noqa: E731
    fast_client.subscribe("*", handler)
    try:
        ev = await fast_client.send_message(ROOM, "@me:localhost", "events!")
        assert any(e.kind == "MessageUpserted" and e.local_echo_id == ev.local_echo_id
                   for e in received)

        async def ack_seen():
            return any(e.kind == "MessageAcknowledged" and e.local_echo_id == ev.local_echo_id
                       for e in received)

        assert await wait_until(ack_seen, timeout=5.0)
    finally:
        fast_client.unsubscribe("*", handler)


async def test_rebuild_outbox_after_restart(fast_client):
    """Simulate a crash: pending messages must re-enter the queue."""
    ev = await fast_client.send_message(ROOM, "@me:localhost", "crash survivor")
    # Do NOT wait for ack; simulate restart by re-arming the outbox.
    count = await fast_client.optimistic.rebuild_outbox()
    assert count >= 1

    async def acked():
        m = next(
            m for m in await fast_client.get_messages(ROOM)
            if m["local_echo_id"] == ev.local_echo_id
        )
        return m["event_id"] is not None

    assert await wait_until(acked, timeout=5.0)


async def test_room_ops_are_optimistic(fast_client):
    room = await fast_client.join_room("!new:localhost", name="New")
    assert room.joined
    rooms = await fast_client.get_rooms()
    assert any(r["room_id"] == "!new:localhost" for r in rooms)

    left = await fast_client.leave_room("!new:localhost")
    assert not left.joined


async def test_notifications_optimistic(fast_client):
    await fast_client.join_room("!prefs:localhost", name="Prefs")
    ev = await fast_client.set_room_notifications(
        "!prefs:localhost", {"mentions_only": True}
    )
    room = await fast_client.get_room("!prefs:localhost")
    assert room["notifications"] == {"mentions_only": True}
