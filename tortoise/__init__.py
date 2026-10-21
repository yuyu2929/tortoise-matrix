"""Tortoise: a slow-friendly Matrix chat client.

Personal use is free (AGPL-3.0). Organizations use the same core under a
commercial license - see COMMERCIAL.md.
"""

from .client import TortoiseClient
from .core import (
    EventBus,
    LocalStore,
    MessageAcknowledged,
    MessageFailed,
    MessageRejected,
    MessageStatus,
    MessageUpserted,
    MockMatrixServer,
    PermanentError,
    QueueMetrics,
    RealMatrixTransport,
    RequestQueue,
    RetryableError,
    RoomUpserted,
    SyncEngine,
    SyncState,
    SyncStateChanged,
    TortoiseEvent,
    Transport,
)

__version__ = "0.1.0"

__all__ = [
    "TortoiseClient",
    "EventBus",
    "LocalStore",
    "MessageAcknowledged",
    "MessageFailed",
    "MessageRejected",
    "MessageStatus",
    "MessageUpserted",
    "MockMatrixServer",
    "PermanentError",
    "QueueMetrics",
    "RealMatrixTransport",
    "RequestQueue",
    "RetryableError",
    "RoomUpserted",
    "SyncEngine",
    "SyncState",
    "SyncStateChanged",
    "TortoiseEvent",
    "Transport",
    "__version__",
]
