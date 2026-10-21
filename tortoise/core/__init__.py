"""Tortoise core: local-first state, optimistic updates, queued delivery,
background sync and pluggable transports."""

from .events import (
    EventBus,
    MessageAcknowledged,
    MessageFailed,
    MessageRejected,
    MessageStatus,
    MessageUpserted,
    QueueMetrics,
    RoomUpserted,
    SyncState,
    SyncStateChanged,
    TortoiseEvent,
    new_echo_id,
)
from .localstore import LocalStore
from .optimistic import OptimisticEngine
from .queue import (
    BACKPRESSURE_HIGH_WATER,
    PRIORITY_MAINTENANCE,
    PRIORITY_SYNC,
    PRIORITY_USER,
    RETRY_SCHEDULE,
    PermanentError,
    RequestQueue,
    RetryableError,
)
from .sync import SyncEngine
from .transport import MockMatrixServer, RealMatrixTransport, Transport

__all__ = [
    "EventBus",
    "LocalStore",
    "MessageAcknowledged",
    "MessageFailed",
    "MessageRejected",
    "MessageStatus",
    "MessageUpserted",
    "MockMatrixServer",
    "OptimisticEngine",
    "PermanentError",
    "PRIORITY_MAINTENANCE",
    "PRIORITY_SYNC",
    "PRIORITY_USER",
    "QueueMetrics",
    "RealMatrixTransport",
    "RequestQueue",
    "RETRY_SCHEDULE",
    "BACKPRESSURE_HIGH_WATER",
    "RetryableError",
    "RoomUpserted",
    "SyncEngine",
    "SyncState",
    "SyncStateChanged",
    "TortoiseEvent",
    "Transport",
    "new_echo_id",
]
