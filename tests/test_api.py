"""End-to-end API tests over HTTP (FastAPI TestClient)."""
from __future__ import annotations

import pytest

from tortoise import MockMatrixServer, TortoiseClient
from tortoise.server import create_app

ROOM_ENC = "%21test%3Alocalhost"
ROOM = "!test:localhost"


@pytest.fixture
async def api():
    client = TortoiseClient(
        transport=MockMatrixServer(latency=0.8, jitter=0.2),
        local_user="@me:localhost",
        poll_interval=0.05,
        retry_schedule=[0.05, 0.1],
    )
    await client.start()
    await client.join_room(ROOM, name="Test Room")
    app = create_app(client)
    from fastapi.testclient import TestClient

    with TestClient(app) as tc:
        yield tc, client
    await client.stop()


async def test_send_returns_pending_immediately(api):
    tc, client = api
    import time

    t0 = time.perf_counter()
    resp = tc.post(
        f"/api/rooms/{ROOM_ENC}/messages", json={"body": "hello api"}
    )
    elapsed = time.perf_counter() - t0
    assert resp.status_code == 201
    data = resp.json()
    assert data["status"] == "pending"
    assert data["event_id"] is None
    assert elapsed < 0.5  # server latency is 0.8s; response must beat it


async def test_get_messages_is_local(api):
    tc, client = api
    tc.post(f"/api/rooms/{ROOM_ENC}/messages", json={"body": "one"})
    import time

    t0 = time.perf_counter()
    resp = tc.get(f"/api/rooms/{ROOM_ENC}/messages")
    elapsed = time.perf_counter() - t0
    assert resp.status_code == 200
    assert any(m["body"] == "one" for m in resp.json())
    assert elapsed < 0.2


async def test_status_endpoint_shape(api):
    tc, client = api
    resp = tc.get("/api/status")
    assert resp.status_code == 200
    data = resp.json()
    assert "sync_state" in data and "queue" in data and "messages" in data
    assert data["enterprise"] is False


async def test_edit_and_delete_endpoints(api):
    tc, client = api
    ev = tc.post(f"/api/rooms/{ROOM_ENC}/messages", json={"body": "v1"}).json()
    ref = ev["local_echo_id"]
    patched = tc.patch(f"/api/rooms/{ROOM_ENC}/messages/{ref}", json={"body": "v2"})
    assert patched.status_code == 200
    assert patched.json()["body"] == "v2"
    deleted = tc.delete(f"/api/rooms/{ROOM_ENC}/messages/{ref}")
    assert deleted.status_code == 200
    msgs = tc.get(f"/api/rooms/{ROOM_ENC}/messages").json()
    row = next(m for m in msgs if m["local_echo_id"] == ref)
    assert row["redacted"]


async def test_notifications_endpoint(api):
    tc, client = api
    resp = tc.post(
        f"/api/rooms/{ROOM_ENC}/notifications",
        json={"prefs": {"mentions_only": True}},
    )
    assert resp.status_code == 200
    rooms = tc.get("/api/rooms").json()
    room = next(r for r in rooms if r["room_id"] == ROOM)
    assert room["notifications"] == {"mentions_only": True}


async def test_websocket_streams_events(api):
    tc, client = api
    with tc.websocket_connect("/ws") as ws:
        tc.post(f"/api/rooms/{ROOM_ENC}/messages", json={"body": "over ws"})
        kinds = set()
        for _ in range(10):
            msg = ws.receive_json()
            kinds.add(msg["kind"])
            if "MessageUpserted" in kinds and msg.get("body") == "over ws":
                break
        assert "MessageUpserted" in kinds
