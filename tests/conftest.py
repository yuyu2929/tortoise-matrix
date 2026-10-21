"""Shared fixtures for the Tortoise test-suite."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402

from tortoise import MockMatrixServer, TortoiseClient  # noqa: E402

ROOM = "!test:localhost"


async def _wait_for_server_room(client, room_id: str, timeout: float = 5.0) -> None:
    """join_room is optimistic; tests that talk to the *server side* must
    wait until the mock homeserver actually knows the room."""
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if room_id in client.transport.rooms:
            return
        await asyncio.sleep(0.02)
    raise TimeoutError(f"server never created {room_id}")


@pytest.fixture
async def fast_client():
    """A client on a near-instant mock server."""
    async with TortoiseClient(
        transport=MockMatrixServer(latency=0.05, jitter=0.01),
        local_user="@me:localhost",
        poll_interval=0.05,
        retry_schedule=[0.05, 0.1, 0.2],
    ) as c:
        await c.join_room(ROOM, name="Test Room")
        await _wait_for_server_room(c, ROOM)
        yield c


@pytest.fixture
async def slow_client():
    """A client on a 2-second-latency mock server: the whole point."""
    async with TortoiseClient(
        transport=MockMatrixServer(latency=2.0, jitter=0.5, seed=42),
        local_user="@me:localhost",
        poll_interval=0.2,
        retry_schedule=[0.2, 0.4, 0.8],
    ) as c:
        await c.join_room(ROOM, name="Slow Room")
        await _wait_for_server_room(c, ROOM, timeout=10.0)
        yield c
