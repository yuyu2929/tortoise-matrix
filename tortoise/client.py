"""Tortoise: the slow-friendly Matrix client.

Public API::

    from tortoise import TortoiseClient, MockMatrixServer

    async with TortoiseClient(transport=MockMatrixServer(latency=3.0)) as c:
        await c.join_room("!room:localhost", name="Slow Server Stress Test")
        event = await c.send_message("!room:localhost", "@me:localhost", "hello")
        # `event` is available *instantly* - the server is still thinking.

Everything that mutates returns the local view immediately; everything
that reads never touches the network. See ARCHITECTURE.md for the design.
"""
from __future__ import annotations

import asyncio
from typing import Any, AsyncIterator, Optional

from .core import (
    EventBus,
    LocalStore,
    MessageStatus,
    MessageUpserted,
    MockMatrixServer,
    OptimisticEngine,
    RequestQueue,
    RetryableError,
    RoomUpserted,
    SyncEngine,
    TortoiseEvent,
    Transport,
)

__version__ = "0.1.0"

__all__ = [
    "TortoiseClient",
    "MockMatrixServer",
    "Transport",
    "MessageStatus",
    "TortoiseEvent",
    "MessageUpserted",
    "RoomUpserted",
    "__version__",
]


class TortoiseClient:
    """The one object an application (CLI, GUI, API server) needs.

    Lifecycle::

        client = TortoiseClient()
        await client.start()
        ...
        await client.stop()

    Or as an async context manager. Reads are always local and instant;
    writes are optimistic and background-delivered.
    """

    def __init__(
        self,
        db_path: str = ":memory:",
        transport: Optional[Transport] = None,
        local_user: str = "@me:localhost",
        queue_workers: int = 4,
        poll_interval: float = 0.5,
        retry_schedule: Optional[list[float]] = None,
    ) -> None:
        self.db_path = db_path
        self.transport = transport or MockMatrixServer(latency=0.2, jitter=0.1)
        self.local_user = local_user
        self.bus = EventBus()
        self.store = LocalStore(db_path)
        self.queue = RequestQueue(
            bus=self.bus, workers=queue_workers, retry_schedule=retry_schedule
        )
        self.optimistic = OptimisticEngine(
            self.store, self.queue, self.transport, self.bus
        )
        self.sync = SyncEngine(
            self.store,
            self.queue,
            self.transport,
            self.bus,
            local_user=local_user,
            poll_interval=poll_interval,
        )
        self._started = False
        self._sync_state = "idle"
        self._sync_detail = ""
        self.bus.subscribe("SyncStateChanged", self._on_sync_state)

    def _on_sync_state(self, event: Any) -> None:
        self._sync_state = event.state
        self._sync_detail = event.detail

    # ------------------------------------------------------------- lifecycle

    async def start(self) -> None:
        if self._started:
            return
        await self.store.connect()
        await self.queue.start()
        await self.sync.start()
        self._started = True
        # Re-arm anything a previous session never delivered.
        await self.optimistic.rebuild_outbox()

    async def stop(self) -> None:
        if not self._started:
            return
        await self.sync.stop()
        await self.queue.stop()
        await self.store.close()
        self._started = False

    async def __aenter__(self) -> "TortoiseClient":
        await self.start()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.stop()

    # ------------------------------------------------------- reads: instant

    async def get_rooms(self) -> list[dict[str, Any]]:
        """Local read. Microseconds. Never blocks on the network."""
        return await self.store.get_rooms()

    async def get_room(self, room_id: str) -> Optional[dict[str, Any]]:
        return await self.store.get_room(room_id)

    async def get_messages(self, room_id: str, limit: int = 100) -> list[dict[str, Any]]:
        """Local read. The UI's message list always comes from here."""
        return await self.store.get_messages(room_id, limit)

    async def status(self) -> dict[str, Any]:
        """Health snapshot for dashboards and the demo UI."""
        counts = await self.store.count_by_status()
        return {
            "sync_state": self.sync_state,
            "sync_rounds": self.sync.rounds_completed,
            "last_sync_error": self.sync.last_error,
            "queue": {
                "depth": self.queue.depth,
                "in_flight": self.queue.in_flight,
                "completed": self.queue.completed,
                "retried": self.queue.retried,
                "dropped_background": self.queue.dropped_background,
                "avg_wait_ms": round(self.queue.avg_wait_ms(), 3),
            },
            "messages": counts,
        }

    @property
    def sync_state(self) -> str:
        return self._sync_state

    @property
    def sync_detail(self) -> str:
        return self._sync_detail

    # ------------------------------------------- writes: optimistic, instant

    async def send_message(self, room_id: str, sender: str, body: str) -> MessageUpserted:
        """Send a message. Returns the local event immediately."""
        return await self.optimistic.send_message(room_id, sender, body)

    async def edit_message(self, room_id: str, ref: str, body: str) -> MessageUpserted:
        return await self.optimistic.edit_message(room_id, ref, body)

    async def delete_message(self, room_id: str, ref: str) -> MessageUpserted:
        return await self.optimistic.delete_message(room_id, ref)

    async def join_room(self, room_id: str, name: str = "") -> RoomUpserted:
        return await self.optimistic.join_room(room_id, name)

    async def leave_room(self, room_id: str) -> RoomUpserted:
        return await self.optimistic.leave_room(room_id)

    async def set_room_notifications(
        self, room_id: str, prefs: dict[str, Any]
    ) -> RoomUpserted:
        return await self.optimistic.set_room_notifications(room_id, prefs)

    async def mark_read(self, room_id: str) -> None:
        await self.optimistic.mark_read(room_id)

    # ------------------------------------------------------------ subscribe

    def subscribe(self, kind: str, handler) -> None:
        """Subscribe to a specific event kind, or ``"*"`` for everything."""
        self.bus.subscribe(kind, handler)

    def unsubscribe(self, kind: str, handler) -> None:
        self.bus.unsubscribe(kind, handler)

    async def events(self) -> AsyncIterator[TortoiseEvent]:
        """Async-generator view of the event stream."""
        q: asyncio.Queue[TortoiseEvent] = asyncio.Queue()

        def _push(event: TortoiseEvent) -> None:
            q.put_nowait(event)

        self.bus.subscribe("*", _push)
        try:
            while True:
                yield await q.get()
        finally:
            self.bus.unsubscribe("*", _push)
