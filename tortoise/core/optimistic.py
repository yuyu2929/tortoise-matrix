"""The optimistic engine: how user actions become instant local events.

The contract with the UI is simple:

* Every mutating call returns **immediately** with the local view of the
  change. The caller never awaits the network.
* The network work is enqueued on the request queue. Success, retryable
  failure and permanent failure each produce a distinct follow-up event so
  the UI can show the true state (clock icon -> checkmark / warning).

Ordering guarantee: all operations on the *same message* share one queue
key, so an edit is never delivered before the original send is acknowledged.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any, Awaitable, Callable, Optional

from .events import (
    EventBus,
    MessageAcknowledged,
    MessageFailed,
    MessageRejected,
    MessageStatus,
    MessageUpserted,
    RoomUpserted,
    new_echo_id,
)
from .localstore import LocalStore
from .queue import PRIORITY_MAINTENANCE, PRIORITY_USER, PermanentError, RequestQueue, RetryableError

#: How long an edit/delete waits for the original send to be acknowledged
#: before giving up and retrying the wait.
ACK_WAIT_TIMEOUT = 300.0


class OptimisticEngine:
    """Turns user intent into local state now, network traffic later."""

    def __init__(
        self,
        store: LocalStore,
        queue: RequestQueue,
        transport: Any,
        bus: EventBus,
    ) -> None:
        self.store = store
        self.queue = queue
        self.transport = transport
        self.bus = bus
        self._ack_events: dict[str, asyncio.Event] = {}
        self._echo_of_event: dict[str, str] = {}  # event_id -> echo, after ack

    # ------------------------------------------------------------------ send

    async def send_message(
        self, room_id: str, sender: str, body: str
    ) -> MessageUpserted:
        """Send a message. Returns as soon as it is visible locally."""
        echo = new_echo_id()
        row = await self.store.insert_local_message(room_id, sender, body, echo)
        event = self._message_event(row)
        self.bus.emit(event)  # UI renders right now

        await self.queue.enqueue(
            factory=lambda: self._deliver(echo, room_id, sender),
            key=f"msg:{echo}",
            priority=PRIORITY_USER,
            on_success=lambda resp: self._on_delivered(echo, resp),
            on_retryable_failure=lambda exc, attempt: self._on_retry(echo, exc, attempt),
            on_permanent_failure=lambda exc: self._on_rejected(echo, exc),
        )
        return event

    async def _deliver(self, echo: str, room_id: str, sender: str) -> dict[str, Any]:
        """Read the *current* local body, so an edit made while the message
        was still pending rides along with the original send."""
        row = await self.store.get_message_by_echo(echo)
        if row is None:
            raise PermanentError("message vanished locally")
        if row["redacted"]:
            # User deleted it before it ever reached the server.
            return {"event_id": "", "ts": time.time(), "skipped": True}
        return await self.transport.send_message(room_id, sender, row["body"])

    async def _on_delivered(self, echo: str, resp: dict[str, Any]) -> None:
        if resp.get("skipped"):
            return
        await self.store.acknowledge_message(echo, resp["event_id"], resp.get("ts"))
        self._echo_of_event[resp["event_id"]] = echo
        self._ack_events.setdefault(echo, asyncio.Event()).set()
        self.bus.emit(
            MessageAcknowledged(
                local_echo_id=echo,
                event_id=resp["event_id"],
                room_id="",
                server_ts=resp.get("ts"),
            )
        )
        self.bus.emit(await self._reload_event(echo))

    async def _on_retry(self, echo: str, exc: Exception, attempt: int) -> None:
        await self.store.mark_failed(echo, attempt)
        delay = self.queue._backoff(attempt) if attempt >= 1 else 1.0
        self.bus.emit(
            MessageFailed(
                local_echo_id=echo,
                room_id="",
                reason=str(exc),
                retryable=True,
                retry_count=attempt,
                next_attempt_in=delay,
            )
        )
        self.bus.emit(await self._reload_event(echo))

    async def _on_rejected(self, echo: str, exc: Exception) -> None:
        await self.store.mark_rejected(echo)
        self.bus.emit(
            MessageRejected(local_echo_id=echo, room_id="", reason=str(exc))
        )
        self.bus.emit(await self._reload_event(echo))

    # ------------------------------------------------------------------ edit

    async def edit_message(self, room_id: str, ref: str, body: str) -> MessageUpserted:
        """Edit a message. The UI sees the new body immediately."""
        ok = await self.store.update_message_body(ref, body)
        if not ok:
            raise KeyError(f"no message {ref!r}")
        event = await self._reload_event(ref)
        self.bus.emit(event)

        echo = await self._resolve_echo(ref)
        await self.queue.enqueue(
            factory=lambda: self._deliver_edit(ref, room_id, body),
            key=f"msg:{echo}",
            priority=PRIORITY_USER,
            on_retryable_failure=lambda exc, attempt: self._on_edit_retry(
                ref, exc, attempt
            ),
        )
        return event

    async def _deliver_edit(self, ref: str, room_id: str, body: str) -> None:
        event_id = await self._await_event_id(ref)
        await self.transport.edit_message(room_id, event_id, body)

    async def _on_edit_retry(self, ref: str, exc: Exception, attempt: int) -> None:
        # The local body is already applied; nothing to do beyond metrics.
        self.bus.emit(
            MessageFailed(
                local_echo_id=ref,
                room_id="",
                reason=f"edit retry: {exc}",
                retryable=True,
                retry_count=attempt,
            )
        )

    # ---------------------------------------------------------------- delete

    async def delete_message(self, room_id: str, ref: str) -> MessageUpserted:
        """Delete a message. Locally it disappears immediately."""
        await self.store.redact_message(ref)
        event = await self._reload_event(ref)
        self.bus.emit(event)

        echo = await self._resolve_echo(ref)
        await self.queue.enqueue(
            factory=lambda: self._deliver_redact(ref, room_id),
            key=f"msg:{echo}",
            priority=PRIORITY_USER,
            on_retryable_failure=lambda exc, attempt: self._on_edit_retry(
                ref, f"delete retry: {exc}", attempt
            ),
        )
        return event

    async def _deliver_redact(self, ref: str, room_id: str) -> None:
        event_id = await self._await_event_id(ref)
        await self.transport.redact_message(room_id, event_id)

    # ----------------------------------------------------------------- rooms

    async def join_room(self, room_id: str, name: str = "") -> RoomUpserted:
        room = await self.store.upsert_room(room_id, name=name, joined=True)
        self.bus.emit(self._room_event(room))
        await self.queue.enqueue(
            factory=lambda: self.transport.join_room(room_id, name),
            key=f"room:{room_id}",
            priority=PRIORITY_USER,
        )
        return self._room_event(room)

    async def leave_room(self, room_id: str) -> RoomUpserted:
        room = await self.store.get_room(room_id)
        if room is not None:
            room = await self.store.upsert_room(
                room_id, name=room["name"], topic=room["topic"], joined=False
            )
        self.bus.emit(self._room_event(room))
        await self.queue.enqueue(
            factory=lambda: self.transport.leave_room(room_id),
            key=f"room:{room_id}",
            priority=PRIORITY_USER,
        )
        return self._room_event(room)

    async def set_room_notifications(
        self, room_id: str, prefs: dict[str, Any]
    ) -> RoomUpserted:
        await self.store.set_room_notifications(room_id, prefs)
        room = await self.store.get_room(room_id)
        self.bus.emit(self._room_event(room))
        await self.queue.enqueue(
            factory=lambda: self.transport.set_room_notifications(room_id, prefs),
            key=f"prefs:{room_id}",
            priority=PRIORITY_MAINTENANCE,
        )
        return self._room_event(room)

    async def mark_read(self, room_id: str) -> None:
        await self.store.mark_room_read(room_id)

    # ------------------------------------------------------------ outbox resync

    async def rebuild_outbox(self) -> int:
        """After a restart, re-arm every message that never got acknowledged."""
        pending = await self.store.pending_messages()
        for row in pending:
            echo = row["local_echo_id"]
            # Bind the row *now*: a plain closure would capture the loop
            # variable and deliver every re-armed message with the LAST
            # row's room/sender.
            room_id = row["room_id"]
            sender = row["sender"]
            await self.queue.enqueue(
                factory=lambda e=echo, r=room_id, s=sender: self._deliver(e, r, s),
                key=f"msg:{echo}",
                priority=PRIORITY_USER,
                on_success=lambda resp, e=echo: self._on_delivered(e, resp),
                on_retryable_failure=lambda exc, attempt, e=echo: self._on_retry(
                    e, exc, attempt
                ),
                on_permanent_failure=lambda exc, e=echo: self._on_rejected(e, exc),
            )
        return len(pending)

    # ---------------------------------------------------------------- helpers

    async def _await_event_id(self, ref: str) -> str:
        """Block (in the background worker, never the UI) until the original
        send has been acknowledged and we know the real event id."""
        row = await self.store.get_message_by_echo(ref)
        if row is None:
            row = await self.store.get_message_by_event_id(ref)
        if row is None:
            raise PermanentError(f"unknown message {ref!r}")
        if row["event_id"]:
            return row["event_id"]

        echo = row["local_echo_id"]
        event = self._ack_events.setdefault(echo, asyncio.Event())
        try:
            await asyncio.wait_for(event.wait(), timeout=ACK_WAIT_TIMEOUT)
        except asyncio.TimeoutError:
            raise RetryableError("still waiting for the original send to land")
        fresh = await self.store.get_message_by_echo(echo)
        if fresh is None or not fresh["event_id"]:
            raise RetryableError("acknowledged but event id missing")
        return fresh["event_id"]

    async def _resolve_echo(self, ref: str) -> str:
        row = await self.store.get_message_by_echo(ref)
        if row is not None:
            return row["local_echo_id"]
        row = await self.store.get_message_by_event_id(ref)
        if row is not None:
            return row["local_echo_id"]
        return ref

    async def _reload_event(self, ref: str) -> MessageUpserted:
        row = await self.store.get_message_by_echo(ref)
        if row is None:
            row = await self.store.get_message_by_event_id(ref)
        if row is None:
            raise KeyError(f"no message {ref!r}")
        return self._message_event(row)

    def _message_event(self, row: dict[str, Any]) -> MessageUpserted:
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

    def _room_event(self, room: Optional[dict[str, Any]]) -> RoomUpserted:
        if room is None:
            return RoomUpserted()
        return RoomUpserted(
            room_id=room["room_id"],
            name=room.get("name", ""),
            topic=room.get("topic", ""),
            joined=bool(room.get("joined")),
            unread=room.get("unread", 0),
        )
