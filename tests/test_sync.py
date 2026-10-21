"""Sync engine tests: incoming messages, edits, redactions and gaps."""
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


async def test_peer_messages_arrive_via_sync(fast_client):
    await fast_client.transport.peer_says(ROOM, "@bob:localhost", "hello from bob", after=0.0)

    async def arrived():
        msgs = await fast_client.get_messages(ROOM)
        return any(m["sender"] == "@bob:localhost" for m in msgs)

    assert await wait_until(arrived, timeout=5.0)
    msgs = await fast_client.get_messages(ROOM)
    row = next(m for m in msgs if m["sender"] == "@bob:localhost")
    assert row["status"] == "received"


async def test_unread_increments_for_remote_only(fast_client):
    await fast_client.transport.peer_says(ROOM, "@bob:localhost", "ping", after=0.0)

    async def unread():
        room = await fast_client.store.get_room(ROOM)
        return room and room["unread"] >= 1

    assert await wait_until(unread, timeout=5.0)


async def test_server_edit_applies(fast_client):
    resp = await fast_client.transport.send_message(ROOM, "@bob:localhost", "typo")
    event_id = resp["event_id"]

    async def received():
        msgs = await fast_client.get_messages(ROOM)
        return any(m["event_id"] == event_id for m in msgs)

    assert await wait_until(received, timeout=5.0)
    await fast_client.transport.edit_message(ROOM, event_id, "fixed")

    async def edited():
        msgs = await fast_client.get_messages(ROOM)
        row = next(m for m in msgs if m["event_id"] == event_id)
        return row["body"] == "fixed" and row["edited"]

    assert await wait_until(edited, timeout=5.0)


async def test_server_redaction_applies(fast_client):
    resp = await fast_client.transport.send_message(ROOM, "@bob:localhost", "will vanish")
    event_id = resp["event_id"]

    async def received():
        msgs = await fast_client.get_messages(ROOM)
        return any(m["event_id"] == event_id for m in msgs)

    assert await wait_until(received, timeout=5.0)
    await fast_client.transport.redact_message(ROOM, event_id)

    async def redacted():
        msgs = await fast_client.get_messages(ROOM)
        row = next(m for m in msgs if m["event_id"] == event_id)
        return row["redacted"]

    assert await wait_until(redacted, timeout=5.0)


async def test_gap_catchup_burst(fast_client):
    """A burst larger than one sync round must still land completely."""
    limit = fast_client.transport.sync_limit  # default 50
    total = limit + 10
    for i in range(total):
        await fast_client.transport.peer_says(
            ROOM, "@bob:localhost", f"burst-{i}", after=0.0
        )

    async def all_landed():
        msgs = await fast_client.get_messages(ROOM, limit=1000)
        bodies = [m["body"] for m in msgs if m["sender"] == "@bob:localhost"]
        return len([b for b in bodies if b.startswith("burst-")]) == total

    assert await wait_until(all_landed, timeout=15.0, interval=0.2)


async def test_sync_state_transitions(fast_client):
    states = []
    fast_client.subscribe("SyncStateChanged", lambda ev: states.append(ev.state))
    await asyncio.sleep(0.3)
    assert "syncing" in states
    assert "live" in states or "lagging" in states
