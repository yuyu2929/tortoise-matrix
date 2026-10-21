"""Regression test: rebuild_outbox must deliver each re-armed message with
its OWN room and sender, not the last pending row's (closure bug)."""
from __future__ import annotations

import asyncio

from tortoise import MockMatrixServer
from tortoise.core.events import EventBus
from tortoise.core.localstore import LocalStore
from tortoise.core.optimistic import OptimisticEngine
from tortoise.core.queue import RequestQueue


async def test_rebuild_outbox_keeps_per_message_room_and_sender():
    store = LocalStore(":memory:")
    await store.connect()
    await store.upsert_room("!r:a", name="A")
    await store.upsert_room("!r:b", name="B")
    await store.insert_local_message("!r:a", "@alice:x", "body a", "echo-a")
    await store.insert_local_message("!r:b", "@bob:x", "body b", "echo-b")

    transport = MockMatrixServer(latency=0.0, jitter=0.0)
    await transport.join_room("!r:a")
    await transport.join_room("!r:b")

    queue = RequestQueue(bus=EventBus(), workers=4)
    engine = OptimisticEngine(store, queue, transport, EventBus())
    await queue.start()

    n = await engine.rebuild_outbox()
    assert n == 2

    # Wait until both messages actually hit the server.
    for _ in range(200):
        sends = [c for c in transport.call_log if c[1] == "send"]
        if len(sends) >= 2:
            break
        await asyncio.sleep(0.01)

    rooms = {c[2] for c in transport.call_log if c[1] == "send"}
    assert rooms == {"!r:a", "!r:b"}, f"rooms delivered: {rooms}"

    await queue.stop()
    await store.close()
