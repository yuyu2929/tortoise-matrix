"""Event types for the Tortoise client.

Every state change in Tortoise is represented as an immutable event object.
The UI layer only ever *consumes* events - it never awaits the network.
This module defines the vocabulary shared by every other module.
"""
from __future__ import annotations

import asyncio
import inspect
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Optional


class MessageStatus(str, Enum):
    """Lifecycle of a message as tracked by the local store.

    PENDING  -> written locally, not yet acknowledged by the server
    SENT     -> server acknowledged with a real event id
    FAILED   -> last delivery attempt failed; still eligible for retry
    REJECTED -> server permanently refused (bad request); do not retry
    RECEIVED -> arrived from the server via background sync
    """

    PENDING = "pending"
    SENT = "sent"
    FAILED = "failed"
    REJECTED = "rejected"
    RECEIVED = "received"


class SyncState(str, Enum):
    """Coarse-grained state of the background sync engine."""

    IDLE = "idle"          # not started
    SYNCING = "syncing"    # a sync round-trip is in flight (server may be slow)
    LIVE = "live"          # caught up with the server
    LAGGING = "lagging"    # server slow or unreachable, retrying in background
    STOPPED = "stopped"


@dataclass(frozen=True)
class TortoiseEvent:
    """Base class for all events.

    ``seq`` is a process-monotonic sequence number assigned by the EventBus
    so consumers can order events without relying on wall clocks.
    """

    seq: int = 0
    ts: float = field(default_factory=time.time)

    @property
    def kind(self) -> str:
        return type(self).__name__


@dataclass(frozen=True)
class MessageUpserted(TortoiseEvent):
    """A message was created or updated locally. The UI should (re)render it."""

    event_id: Optional[str] = None
    room_id: str = ""
    sender: str = ""
    body: str = ""
    status: str = MessageStatus.PENDING.value
    local_echo_id: Optional[str] = None
    edited: bool = False
    redacted: bool = False
    ts: float = 0.0


@dataclass(frozen=True)
class MessageAcknowledged(TortoiseEvent):
    """A previously pending message was confirmed by the server."""

    local_echo_id: str = ""
    event_id: str = ""
    room_id: str = ""
    server_ts: Optional[float] = None


@dataclass(frozen=True)
class MessageFailed(TortoiseEvent):
    """A pending message failed its latest delivery attempt."""

    local_echo_id: str = ""
    room_id: str = ""
    reason: str = ""
    retryable: bool = True
    retry_count: int = 0
    next_attempt_in: Optional[float] = None


@dataclass(frozen=True)
class MessageRejected(TortoiseEvent):
    """The server permanently refused a message (do not retry)."""

    local_echo_id: str = ""
    room_id: str = ""
    reason: str = ""


@dataclass(frozen=True)
class RoomUpserted(TortoiseEvent):
    """A room was created, joined, left or updated."""

    room_id: str = ""
    name: str = ""
    topic: str = ""
    joined: bool = True
    unread: int = 0


@dataclass(frozen=True)
class SyncStateChanged(TortoiseEvent):
    """The background sync engine changed state."""

    state: str = SyncState.IDLE.value
    detail: str = ""


@dataclass(frozen=True)
class QueueMetrics(TortoiseEvent):
    """Periodic snapshot of request-queue health (used by the demo UI)."""

    depth: int = 0
    in_flight: int = 0
    completed: int = 0
    retried: int = 0
    dropped_background: int = 0
    avg_wait_ms: float = 0.0


class EventBus:
    """Tiny synchronous pub/sub.

    Deliberately *not* async: emitting an event must never await anything,
    otherwise a slow subscriber could block the sync engine. Async handlers
    are scheduled as fire-and-forget tasks; sync handlers are called inline.
    """

    def __init__(self) -> None:
        self._subscribers: dict[str, list[Callable[[TortoiseEvent], Any]]] = {}
        self._seq = 0

    def subscribe(self, kind: str, handler: Callable[[TortoiseEvent], Any]) -> None:
        """Subscribe to a specific event kind, or ``"*"`` for everything."""
        self._subscribers.setdefault(kind, []).append(handler)

    def unsubscribe(self, kind: str, handler: Callable[[TortoiseEvent], Any]) -> None:
        handlers = self._subscribers.get(kind)
        if handlers and handler in handlers:
            handlers.remove(handler)

    def emit(self, event: TortoiseEvent) -> None:
        """Publish an event. Never raises into the emitter's face."""
        self._seq += 1
        # dataclasses are frozen; replace with a copy carrying the seq
        object.__setattr__(event, "seq", self._seq)
        for kind in (event.kind, "*"):
            for handler in list(self._subscribers.get(kind, ())):
                try:
                    result = handler(event)
                    if _is_awaitable(result):
                        asyncio.ensure_future(result)
                except Exception:
                    # A misbehaving subscriber must never break the engine.
                    pass


def _is_awaitable(obj: Any) -> bool:
    return inspect.iscoroutine(obj) or isinstance(obj, asyncio.Future)


def new_echo_id() -> str:
    """Client-side temporary id for an optimistic message."""
    return f"echo-{uuid.uuid4().hex[:16]}"


def new_event_id() -> str:
    """Server-style opaque event id (used by the mock server)."""
    return f"${uuid.uuid4().hex}"
