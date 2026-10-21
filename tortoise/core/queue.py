"""The request queue: every network operation in Tortoise flows through here.

Design rules that make the client "slow-friendly":

1. **User actions always win.** Priority 0 (user) tasks are never dropped,
   never coalesced away, and retried forever with exponential backoff.
2. **Background tasks are expendable.** Sync/polling tasks (priority > 0)
   may be dropped under backpressure - missing one sync round costs nothing
   because the next round catches up.
3. **Nothing blocks.** ``enqueue`` returns immediately; workers run as
   independent asyncio tasks. A slow server delays one worker, not the UI.
4. **Coalescing.** Idempotent background work (e.g. "sync now") collapses
   when an equivalent task is already in flight.
"""
from __future__ import annotations

import asyncio
import heapq
import itertools
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

from .events import EventBus, QueueMetrics

#: Priority classes. Lower number = served first.
PRIORITY_USER = 0          # send / edit / delete / join / leave / preferences
PRIORITY_SYNC = 10         # background incremental sync
PRIORITY_MAINTENANCE = 20  # read receipts, typing notifications, etc.

#: Retry schedule (seconds) for retryable failures. Grows to a 60 s cap.
RETRY_SCHEDULE = [1, 2, 4, 8, 16, 32, 60]

#: When the pending queue exceeds this depth, background tasks are dropped.
BACKPRESSURE_HIGH_WATER = 200

#: Cooldown between QueueMetrics emissions (seconds).
METRICS_INTERVAL = 1.0

Callback = Optional[Callable[..., Awaitable[Any]]]


class PermanentError(Exception):
    """The operation can never succeed - do not retry, surface to the user."""


class RetryableError(Exception):
    """The operation failed transiently - retry with backoff."""


@dataclass(order=True)
class _QueueItem:
    priority: int
    seq: int
    enqueued_at: float
    key: str = field(compare=False)
    factory: Callable[[], Awaitable[Any]] = field(compare=False)
    coalescible: bool = field(compare=False, default=False)
    attempts: int = field(compare=False, default=0)
    on_success: Callback = field(compare=False, default=None)
    on_retryable_failure: Callback = field(compare=False, default=None)
    on_permanent_failure: Callback = field(compare=False, default=None)


class RequestQueue:
    """A priority queue with per-task retry, coalescing and backpressure."""

    def __init__(
        self,
        bus: Optional[EventBus] = None,
        workers: int = 4,
        high_water: int = BACKPRESSURE_HIGH_WATER,
        retry_schedule: Optional[list[float]] = None,
    ) -> None:
        self.bus = bus or EventBus()
        self.workers = workers
        self.high_water = high_water
        self.retry_schedule = retry_schedule or RETRY_SCHEDULE
        self._heap: list[_QueueItem] = []
        self._counter = itertools.count()
        self._in_flight: set[str] = set()
        self._wake = asyncio.Event()
        self._stopping = False
        self._worker_tasks: list[asyncio.Task] = []
        self._metrics_task: Optional[asyncio.Task] = None
        # counters
        self.completed = 0
        self.retried = 0
        self.dropped_background = 0
        self._wait_samples: list[float] = []

    # ------------------------------------------------------------- lifecycle

    async def start(self) -> None:
        self._stopping = False
        self._worker_tasks = [
            asyncio.create_task(self._worker_loop(i)) for i in range(self.workers)
        ]
        self._metrics_task = asyncio.create_task(self._metrics_loop())

    async def stop(self, drain_timeout: float = 5.0) -> None:
        """Stop accepting work; give in-flight tasks a moment to finish."""
        self._stopping = True
        self._wake.set()
        if self._worker_tasks:
            _done, pending = await asyncio.wait(
                self._worker_tasks, timeout=drain_timeout
            )
            for t in pending:
                t.cancel()
        if self._metrics_task:
            self._metrics_task.cancel()

    # ---------------------------------------------------------------- enqueue

    async def enqueue(
        self,
        factory: Callable[[], Awaitable[Any]],
        *,
        key: str,
        priority: int = PRIORITY_USER,
        coalescible: bool = False,
        on_success: Callback = None,
        on_retryable_failure: Callback = None,
        on_permanent_failure: Callback = None,
    ) -> bool:
        """Schedule a task. Returns False if it was dropped (background only)."""
        if self._stopping:
            return False

        if coalescible and key in self._in_flight:
            # An equivalent background task is already running; skip this one.
            self.dropped_background += 1
            return False

        if priority > PRIORITY_USER and len(self._heap) >= self.high_water:
            # Backpressure: never drop user work, only background work.
            self.dropped_background += 1
            return False

        item = _QueueItem(
            priority=priority,
            seq=next(self._counter),
            enqueued_at=time.monotonic(),
            key=key,
            factory=factory,
            coalescible=coalescible,
            on_success=on_success,
            on_retryable_failure=on_retryable_failure,
            on_permanent_failure=on_permanent_failure,
        )
        heapq.heappush(self._heap, item)
        self._wake.set()
        return True

    # ---------------------------------------------------------------- workers

    async def _worker_loop(self, worker_id: int) -> None:
        while not self._stopping:
            if not self._heap:
                self._wake.clear()
                await self._wake.wait()
                continue
            item = heapq.heappop(self._heap)
            self._in_flight.add(item.key)
            self._wait_samples.append(time.monotonic() - item.enqueued_at)
            try:
                await self._run_item(item)
            except asyncio.CancelledError:
                raise
            except Exception:
                # A crashing task must never kill the worker.
                pass
            finally:
                self._in_flight.discard(item.key)

    async def _run_item(self, item: _QueueItem) -> None:
        try:
            result = await item.factory()
            self.completed += 1
            if item.on_success is not None:
                await item.on_success(result)
            return
        except PermanentError as exc:
            self.completed += 1
            if item.on_permanent_failure is not None:
                await item.on_permanent_failure(exc)
            return
        except Exception as exc:  # noqa: BLE001 - everything else is retryable
            retryable = (
                exc if isinstance(exc, RetryableError) else RetryableError(str(exc))
            )
            item.attempts += 1
            self.retried += 1
            if item.on_retryable_failure is not None:
                try:
                    await item.on_retryable_failure(retryable, item.attempts)
                except Exception:
                    pass
            delay = self._backoff(item.attempts)
            await asyncio.sleep(delay)
            if not self._stopping:
                item.enqueued_at = time.monotonic()
                heapq.heappush(self._heap, item)
                self._wake.set()

    def _backoff(self, attempt: int) -> float:
        idx = min(attempt - 1, len(self.retry_schedule) - 1)
        return self.retry_schedule[idx]

    # ---------------------------------------------------------------- metrics

    @property
    def depth(self) -> int:
        return len(self._heap)

    @property
    def in_flight(self) -> int:
        return len(self._in_flight)

    def avg_wait_ms(self) -> float:
        if not self._wait_samples:
            return 0.0
        recent = self._wait_samples[-100:]
        return sum(recent) / len(recent) * 1000.0

    async def _metrics_loop(self) -> None:
        while not self._stopping:
            self.bus.emit(
                QueueMetrics(
                    depth=self.depth,
                    in_flight=self.in_flight,
                    completed=self.completed,
                    retried=self.retried,
                    dropped_background=self.dropped_background,
                    avg_wait_ms=self.avg_wait_ms(),
                )
            )
            await asyncio.sleep(METRICS_INTERVAL)
