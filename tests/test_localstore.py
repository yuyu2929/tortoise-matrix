"""LocalStore tests: the single source of truth must be bulletproof."""
from __future__ import annotations

import pytest

from tortoise.core.events import MessageStatus
from tortoise.core.localstore import LocalStore

from conftest import ROOM


@pytest.fixture
async def store():
    s = LocalStore(":memory:")
    await s.connect()
    yield s
    await s.close()


async def test_insert_local_message_is_pending(store):
    row = await store.insert_local_message(ROOM, "@me:x", "hello", "echo-1")
    assert row["status"] == MessageStatus.PENDING.value
    assert row["event_id"] is None
    assert row["local_echo_id"] == "echo-1"


async def test_acknowledge_promotes_to_sent(store):
    await store.insert_local_message(ROOM, "@me:x", "hello", "echo-1")
    ok = await store.acknowledge_message("echo-1", "$abc", 123.0)
    assert ok
    row = await store.get_message_by_echo("echo-1")
    assert row["status"] == MessageStatus.SENT.value
    assert row["event_id"] == "$abc"
    assert row["server_ts"] == 123.0


async def test_acknowledge_is_idempotent_guarded(store):
    await store.insert_local_message(ROOM, "@me:x", "hello", "echo-1")
    await store.acknowledge_message("echo-1", "$abc")
    # second ack for the same echo must not match (already sent)
    ok = await store.acknowledge_message("echo-1", "$def")
    assert not ok
    row = await store.get_message_by_echo("echo-1")
    assert row["event_id"] == "$abc"


async def test_failed_then_requeue_then_sent(store):
    await store.insert_local_message(ROOM, "@me:x", "hello", "echo-1")
    await store.mark_failed("echo-1", retry_count=1)
    assert (await store.get_message_by_echo("echo-1"))["status"] == "failed"
    await store.requeue_pending("echo-1")
    assert (await store.get_message_by_echo("echo-1"))["status"] == "pending"
    await store.acknowledge_message("echo-1", "$ok")
    assert (await store.get_message_by_echo("echo-1"))["status"] == "sent"


async def test_mark_rejected(store):
    await store.insert_local_message(ROOM, "@me:x", "hello", "echo-1")
    await store.mark_rejected("echo-1")
    assert (await store.get_message_by_echo("echo-1"))["status"] == "rejected"


async def test_edit_and_redact(store):
    await store.insert_local_message(ROOM, "@me:x", "v1", "echo-1")
    assert await store.update_message_body("echo-1", "v2")
    row = await store.get_message_by_echo("echo-1")
    assert row["body"] == "v2" and row["edited"]
    assert await store.redact_message("echo-1")
    row = await store.get_message_by_echo("echo-1")
    assert row["redacted"] and row["body"] == ""


async def test_received_messages_idempotent_on_event_id(store):
    a = await store.upsert_received_message("$e1", ROOM, "@bob:x", "hi", 1.0)
    b = await store.upsert_received_message("$e1", ROOM, "@bob:x", "hi", 1.0)
    assert a and not b  # second insert is a no-op


async def test_get_messages_ordered_by_ts(store):
    await store.upsert_received_message("$e1", ROOM, "@bob:x", "first", 1.0)
    await store.upsert_received_message("$e2", ROOM, "@bob:x", "second", 2.0)
    msgs = await store.get_messages(ROOM)
    assert [m["body"] for m in msgs] == ["first", "second"]


async def test_rooms_crud_and_notifications(store):
    await store.upsert_room(ROOM, name="Test")
    room = await store.get_room(ROOM)
    assert room["name"] == "Test" and room["joined"]
    await store.set_room_notifications(ROOM, {"mentions_only": True})
    room = await store.get_room(ROOM)
    assert room["notifications"] == {"mentions_only": True}
    # partial merge keeps earlier keys
    await store.set_room_notifications(ROOM, {"sound": False})
    room = await store.get_room(ROOM)
    assert room["notifications"] == {"mentions_only": True, "sound": False}


async def test_unread_and_mark_read(store):
    await store.upsert_room(ROOM, name="Test")
    await store.bump_unread(ROOM, 3)
    assert (await store.get_room(ROOM))["unread"] == 3
    await store.mark_room_read(ROOM)
    assert (await store.get_room(ROOM))["unread"] == 0


async def test_sync_token_roundtrip(store):
    assert await store.get_sync_token() is None
    await store.set_sync_token("s42")
    assert await store.get_sync_token() == "s42"
    await store.set_sync_token("s43")
    assert await store.get_sync_token() == "s43"


async def test_pending_recovery_after_restart(store):
    await store.insert_local_message(ROOM, "@me:x", "unsent-1", "echo-1")
    await store.insert_local_message(ROOM, "@me:x", "unsent-2", "echo-2")
    await store.acknowledge_message("echo-2", "$done")
    pending = await store.pending_messages()
    assert [p["local_echo_id"] for p in pending] == ["echo-1"]


async def test_delete_messages_before(store):
    await store.upsert_received_message("$old", ROOM, "@bob:x", "old", 1.0)
    await store.upsert_received_message("$new", ROOM, "@bob:x", "new", 9999999999.0)
    n = await store.delete_messages_before(1000.0)
    assert n == 1
    bodies = [m["body"] for m in await store.get_messages(ROOM)]
    assert bodies == ["new"]
