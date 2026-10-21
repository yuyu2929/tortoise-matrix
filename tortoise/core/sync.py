"""The background sync engine.

Runs entirely behind the UI's back: it polls the homeserver for new
events, writes them into the local store, and emits events for the UI.
A slow, laggy or unreachable server changes nothing about the client's
responsiveness - the engine just reports ``lagging`` and keeps trying.

Gap handling: when a sync response is ``limited`` (more events pending
than one round can carry), the next round fires immediately instead of
waiting for the poll interval, so a burst never falls behind.
"""
from __future__ import annotations

import asyncio
from typing import Any, Optional

from .events import (
    EventBus,
    MessageUpserted,
    MessageStatus,
    RoomUpserted,
    SyncState,
    SyncStateChanged,
)
from .localstore import LocalStore
from .queue import PRIORITY_SYNC, RequestQueue


class SyncEngine:
    """Background incremental sync with slow-server tolerance."""

    def __init__(
        self,
        store: LocalStore,
        queue: RequestQueue,
        transport: Any,
        bus: EventBus,
        local_user: str = "@me:localhost",
        poll_interval: float = 0.5,
        sync_timeout: int = 30,
    ) -> None:
        self.store = store
        self.queue = queue
        self.transport = transport
        self.bus = bus
        self.local_user = local_user
        self.poll_interval = poll_interval
        self.sync_timeout = sync_timeout
        self._stop = asyncio.Event()
        self._round_done = asyncio.Event()
        self._round_done.set()
        self._task: Optional[asyncio.Task] = None
        self.rounds_completed = 0
        self.last_error: Optional[str] = None

    # ------------------------------------------------------------- lifecycle

    async def start(self) -> None:
        self._stop.clear()
        self.bus.emit(SyncStateChanged(state=SyncState.SYNCING.value))
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        self._stop.set()
        self._round_done.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self.bus.emit(SyncStateChanged(state=SyncState.STOPPED.value))

    # ------------------------------------------------------------------- loop

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self._round_done.clear()
                enqueued = await self.queue.enqueue(
                    factory=self._sync_once,
                    key="sync",
                    priority=PRIORITY_SYNC,
                    coalescible=True,
                )
                if not enqueued:
                    # Dropped under backpressure (or already in flight).
                    # Wait a beat and loop; the next round catches up.
                    await asyncio.sleep(self.poll_interval)
                    continue

                # Wait for this round to finish (bounded: a wedged sync task
                # must not wedge the loop).
                try:
                    await asyncio.wait_for(
                        self._round_done.wait(),
                        timeout=self.sync_timeout + self.poll_interval + 5,
                    )
                except asyncio.TimeoutError:
                    pass

                if self._stop.is_set():
                    break

                limited = self._last_round_limited
                if limited:
                    continue  # burst pending: immediately run another round

                await asyncio.sleep(self.poll_interval)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self.last_error = str(exc)
                self.bus.emit(
                    SyncStateChanged(state=SyncState.LAGGING.value, detail=str(exc))
                )
                await asyncio.sleep(self.poll_interval)

    _last_round_limited = False

    # ------------------------------------------------------------------- sync

    async def _sync_once(self) -> dict[str, Any]:
        try:
            token = await self.store.get_sync_token()
            self.bus.emit(SyncStateChanged(state=SyncState.SYNCING.value))
            resp = await self.transport.sync(since=token, timeout=self.sync_timeout)
            await self._apply(resp)
            self.rounds_completed += 1
            self.last_error = None
            self._last_round_limited = bool(resp.get("limited"))
            self.bus.emit(
                SyncStateChanged(
                    state=SyncState.LIVE.value,
                    detail=f"round {self.rounds_completed}",
                )
            )
            return resp
        except Exception as exc:  # noqa: BLE001
            self.last_error = str(exc)
            self._last_round_limited = False
            self.bus.emit(
                SyncStateChanged(state=SyncState.LAGGING.value, detail=str(exc))
            )
            raise
        finally:
            self._round_done.set()

    # ------------------------------------------------------------------ apply

    async def _apply(self, resp: dict[str, Any]) -> None:
        # Rooms first, so message inserts always have their room.
        for room in resp.get("rooms", []):
            existing = await self.store.get_room(room["room_id"])
            await self.store.upsert_room(
                room["room_id"],
                name=room.get("name", ""),
                topic=room.get("topic", ""),
                joined=room.get("joined", True),
                notifications=room.get("notifications"),
            )
            self.bus.emit(
                RoomUpserted(
                    room_id=room["room_id"],
                    name=room.get("name", ""),
                    topic=room.get("topic", ""),
                    joined=bool(room.get("joined", True)),
                )
            )

        for ev in resp.get("events", []):
            etype = ev.get("type")
            room_id = ev.get("room_id", "")

            if etype == "m.room.message":
                is_new = await self.store.upsert_received_message(
                    event_id=ev["event_id"],
                    room_id=room_id,
                    sender=ev.get("sender", ""),
                    body=ev.get("body", ""),
                    server_ts=ev.get("ts", 0.0),
                )
                if is_new:
                    if ev.get("sender") != self.local_user:
                        await self.store.bump_unread(room_id)
                    row = await self.store.get_message_by_event_id(ev["event_id"])
                    if row is not None:
                        self.bus.emit(_message_event(row))

            elif etype == "m.room.message.edit":
                await self.store.apply_server_edit(
                    ev["target_event_id"], ev.get("body", "")
                )
                row = await self.store.get_message_by_event_id(ev["target_event_id"])
                if row is not None:
                    self.bus.emit(_message_event(row))

            elif etype == "m.room.redaction":
                await self.store.apply_server_redaction(ev["target_event_id"])
                row = await self.store.get_message_by_event_id(ev["target_event_id"])
                if row is not None:
                    self.bus.emit(_message_event(row))

        if resp.get("next_batch"):
            await self.store.set_sync_token(str(resp["next_batch"]))


def _message_event(row: dict[str, Any]) -> MessageUpserted:
    return MessageUpserted(
        event_id=row["event_id"],
        room_id=row["room_id"],
        sender=row["sender"],
        body="" if row["redacted"] else row["body"],
        status=row["status"],
        local_echo_id=row["local_echo_id"],
        edited=bool(row["edited"]),
        redacted=bool(row["redacted"]),
        ts=row["ts"],
    )
