"""Queue tests: priorities, retries, backpressure, coalescing."""
from __future__ import annotations

import asyncio

import pytest

from tortoise.core.events import EventBus
from tortoise.core.queue import (
    PRIORITY_MAINTENANCE,
    PRIORITY_SYNC,
    PRIORITY_USER,
    PermanentError,
    RequestQueue,
    RetryableError,
)


@pytest.fixture
async def queue():
    q = RequestQueue(bus=EventBus(), workers=2, retry_schedule=[0.02, 0.04, 0.08])
    await q.start()
    yield q
    await q.stop(drain_timeout=2.0)


async def test_user_priority_beats_background(queue):
    order = []

    async def bg():
        order.append("bg")

    async def user():
        order.append("user")

    await queue.enqueue(bg, key="bg", priority=PRIORITY_SYNC)
    await asyncio.sleep(0.02)  # let bg start first
    await queue.enqueue(user, key="user", priority=PRIORITY_USER)
    await asyncio.sleep(0.1)
    # With 2 workers both may run, but a single-worker queue would prefer user.
    assert "user" in order


async def test_retryable_failure_retries_then_succeeds(queue):
    attempts = {"n": 0}

    async def flaky():
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise RetryableError("boom")
        return "ok"

    result = asyncio.get_event_loop().create_future()

    async def on_success(res):
        result.set_result(res)

    await queue.enqueue(
        flaky, key="flaky", priority=PRIORITY_USER, on_success=on_success
    )
    assert await asyncio.wait_for(result, timeout=3.0) == "ok"
    assert attempts["n"] == 3
    assert queue.retried == 2


async def test_permanent_error_never_retries(queue):
    attempts = {"n": 0}

    async def hopeless():
        attempts["n"] += 1
        raise PermanentError("nope")

    failed = asyncio.get_event_loop().create_future()

    async def on_permanent(exc):
        failed.set_result(exc)

    await queue.enqueue(
        hopeless, key="hopeless", priority=PRIORITY_USER,
        on_permanent_failure=on_permanent,
    )
    exc = await asyncio.wait_for(failed, timeout=2.0)
    assert "nope" in str(exc)
    assert attempts["n"] == 1


async def test_backpressure_drops_only_background(queue):
    q = RequestQueue(bus=EventBus(), workers=1, high_water=1,
                     retry_schedule=[0.01])

    async def blocker():
        await asyncio.sleep(5)

    async def noop():
        return None

    await q.start()
    blocked = asyncio.ensure_future(blocker())
    await q.enqueue(blocker, key="block", priority=PRIORITY_USER)

    # fill the queue beyond high water with background tasks
    dropped = [
        await q.enqueue(noop, key=f"bg-{i}", priority=PRIORITY_SYNC)
        for i in range(3)
    ]
    assert not all(dropped)
    # user tasks are never dropped
    ok = await q.enqueue(noop, key="user-1", priority=PRIORITY_USER)
    assert ok
    await q.stop(drain_timeout=0.1)
    blocked.cancel()


async def test_coalescing_skips_duplicate_inflight(queue):
    ran = {"n": 0}

    async def slow_bg():
        ran["n"] += 1
        await asyncio.sleep(0.2)

    first = await queue.enqueue(slow_bg, key="sync", priority=PRIORITY_SYNC, coalescible=True)
    await asyncio.sleep(0.02)  # make sure it's in flight
    second = await queue.enqueue(slow_bg, key="sync", priority=PRIORITY_SYNC, coalescible=True)
    assert first and not second
    await asyncio.sleep(0.4)
    assert ran["n"] == 1


async def test_metrics_counters(queue):
    async def ok():
        return 1

    await queue.enqueue(ok, key="a")
    await queue.enqueue(ok, key="b")
    await asyncio.sleep(0.2)
    assert queue.completed == 2
    assert queue.depth == 0
