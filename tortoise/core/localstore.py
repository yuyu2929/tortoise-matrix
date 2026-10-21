"""Local SQLite state store.

This module is the single source of truth for the UI. Every read the UI
performs hits this store and returns in microseconds - it never touches
the network. Every write the user performs lands here *first*; the network
is told about it afterwards, in the background.

Concurrency model: one aiosqlite connection guarded by an asyncio lock.
SQLite operations on a local file take single-digit milliseconds, so the
lock never produces user-perceptible waiting.
"""
from __future__ import annotations

import json
import time
from typing import Any, Optional

import aiosqlite

from .events import MessageStatus

SCHEMA = """
CREATE TABLE IF NOT EXISTS rooms (
    room_id        TEXT PRIMARY KEY,
    name           TEXT NOT NULL DEFAULT '',
    topic          TEXT NOT NULL DEFAULT '',
    joined         INTEGER NOT NULL DEFAULT 1,
    unread         INTEGER NOT NULL DEFAULT 0,
    notifications  TEXT NOT NULL DEFAULT '{}',
    updated_ts     REAL NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS messages (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id        TEXT UNIQUE,          -- NULL until the server acknowledges
    local_echo_id   TEXT UNIQUE,          -- always present; the optimistic handle
    room_id         TEXT NOT NULL,
    sender          TEXT NOT NULL,
    body            TEXT NOT NULL,
    ts              REAL NOT NULL,        -- local send time (server ts if received)
    server_ts       REAL,                 -- authoritative ts, set on acknowledgement
    status          TEXT NOT NULL,
    edited          INTEGER NOT NULL DEFAULT 0,
    redacted        INTEGER NOT NULL DEFAULT 0,
    retry_count     INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (room_id) REFERENCES rooms(room_id)
);
CREATE INDEX IF NOT EXISTS idx_messages_room_ts ON messages(room_id, ts);

CREATE TABLE IF NOT EXISTS sync_state (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def _row_to_dict(row: aiosqlite.Row) -> dict[str, Any]:
    d = dict(row)
    d["edited"] = bool(d["edited"])
    d["redacted"] = bool(d["redacted"])
    return d


def _row_to_room(row: aiosqlite.Row) -> dict[str, Any]:
    d = dict(row)
    d["joined"] = bool(d["joined"])
    try:
        d["notifications"] = json.loads(d.get("notifications") or "{}")
    except (json.JSONDecodeError, TypeError):
        d["notifications"] = {}
    return d


class LocalStore:
    """Async SQLite wrapper holding rooms, messages and the sync cursor."""

    def __init__(self, db_path: str = ":memory:") -> None:
        self.db_path = db_path
        self._db: Optional[aiosqlite.Connection] = None
        self._lock = None  # created lazily; asyncio.Lock needs a running loop in old versions

    # ------------------------------------------------------------------ setup

    async def connect(self) -> None:
        import asyncio

        if self._db is not None:
            return
        self._lock = asyncio.Lock()
        self._db = await aiosqlite.connect(self.db_path)
        self._db.row_factory = aiosqlite.Row
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.execute("PRAGMA synchronous=NORMAL")
        await self._db.executescript(SCHEMA)
        await self._db.commit()

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    @property
    def db(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("LocalStore.connect() must be awaited first")
        return self._db

    # ------------------------------------------------------------------ rooms

    async def upsert_room(
        self,
        room_id: str,
        name: str = "",
        topic: str = "",
        joined: bool = True,
        unread: int = 0,
        notifications: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        async with self._lock:
            if notifications is None:
                await self.db.execute(
                    """
                    INSERT INTO rooms (room_id, name, topic, joined, unread, updated_ts)
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(room_id) DO UPDATE SET
                        name=excluded.name, topic=excluded.topic,
                        joined=excluded.joined, unread=excluded.unread,
                        updated_ts=excluded.updated_ts
                    """,
                    (room_id, name, topic, int(joined), unread, time.time()),
                )
            else:
                await self.db.execute(
                    """
                    INSERT INTO rooms
                        (room_id, name, topic, joined, unread, notifications, updated_ts)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(room_id) DO UPDATE SET
                        name=excluded.name, topic=excluded.topic,
                        joined=excluded.joined, unread=excluded.unread,
                        notifications=excluded.notifications,
                        updated_ts=excluded.updated_ts
                    """,
                    (
                        room_id,
                        name,
                        topic,
                        int(joined),
                        unread,
                        json.dumps(notifications),
                        time.time(),
                    ),
                )
            await self.db.commit()
        return await self.get_room(room_id)

    async def get_room(self, room_id: str) -> Optional[dict[str, Any]]:
        async with self._lock:
            cur = await self.db.execute(
                "SELECT * FROM rooms WHERE room_id = ?", (room_id,)
            )
            row = await cur.fetchone()
        if row is None:
            return None
        d = _row_to_room(row)
        return d

    async def get_rooms(self) -> list[dict[str, Any]]:
        async with self._lock:
            cur = await self.db.execute(
                "SELECT * FROM rooms ORDER BY updated_ts DESC"
            )
            rows = await cur.fetchall()
        return [_row_to_room(r) for r in rows]

    async def set_room_notifications(self, room_id: str, prefs: dict[str, Any]) -> None:
        """Persist notification preferences (optimistically applied upstream)."""
        room = await self.get_room(room_id)
        if room is None:
            return
        merged = {**room.get("notifications", {}), **prefs}
        await self.upsert_room(
            room_id, name=room["name"], topic=room["topic"], notifications=merged
        )

    async def mark_room_read(self, room_id: str) -> None:
        async with self._lock:
            await self.db.execute(
                "UPDATE rooms SET unread = 0 WHERE room_id = ?", (room_id,)
            )
            await self.db.commit()

    async def bump_unread(self, room_id: str, by: int = 1) -> None:
        async with self._lock:
            await self.db.execute(
                "UPDATE rooms SET unread = unread + ? WHERE room_id = ?", (by, room_id)
            )
            await self.db.commit()

    # ---------------------------------------------------------------- messages

    async def insert_local_message(
        self,
        room_id: str,
        sender: str,
        body: str,
        local_echo_id: str,
        ts: Optional[float] = None,
    ) -> dict[str, Any]:
        """Insert a pending (optimistic) message. This is what the UI renders
        immediately after the user hits send - before any network I/O."""
        ts = ts if ts is not None else time.time()
        async with self._lock:
            await self.db.execute(
                """
                INSERT INTO messages
                    (event_id, local_echo_id, room_id, sender, body, ts, status)
                VALUES (NULL, ?, ?, ?, ?, ?, ?)
                """,
                (local_echo_id, room_id, sender, body, ts, MessageStatus.PENDING.value),
            )
            await self.db.commit()
        return await self.get_message_by_echo(local_echo_id)

    async def acknowledge_message(
        self, local_echo_id: str, event_id: str, server_ts: Optional[float] = None
    ) -> bool:
        """Reconcile an optimistic message with the server's real event id.

        Accepts messages in either ``pending`` (first delivery) or ``failed``
        (a retry finally landed) state."""
        async with self._lock:
            cur = await self.db.execute(
                """
                UPDATE messages
                   SET event_id = ?, server_ts = ?, status = ?, retry_count = 0
                 WHERE local_echo_id = ? AND status IN (?, ?)
                """,
                (
                    event_id,
                    server_ts,
                    MessageStatus.SENT.value,
                    local_echo_id,
                    MessageStatus.PENDING.value,
                    MessageStatus.FAILED.value,
                ),
            )
            await self.db.commit()
            return cur.rowcount > 0

    async def mark_failed(self, local_echo_id: str, retry_count: int) -> bool:
        async with self._lock:
            cur = await self.db.execute(
                "UPDATE messages SET status = ?, retry_count = ? WHERE local_echo_id = ?",
                (MessageStatus.FAILED.value, retry_count, local_echo_id),
            )
            await self.db.commit()
            return cur.rowcount > 0

    async def mark_rejected(self, local_echo_id: str) -> bool:
        async with self._lock:
            cur = await self.db.execute(
                "UPDATE messages SET status = ? WHERE local_echo_id = ?",
                (MessageStatus.REJECTED.value, local_echo_id),
            )
            await self.db.commit()
            return cur.rowcount > 0

    async def requeue_pending(self, local_echo_id: str) -> bool:
        """Flip a FAILED message back to PENDING for another delivery attempt."""
        async with self._lock:
            cur = await self.db.execute(
                "UPDATE messages SET status = ? WHERE local_echo_id = ? AND status = ?",
                (MessageStatus.PENDING.value, local_echo_id, MessageStatus.FAILED.value),
            )
            await self.db.commit()
            return cur.rowcount > 0

    async def update_message_body(self, local_echo_id: str, body: str) -> bool:
        """Optimistic edit: rewrite the body the UI shows, right now.
        Accepts either a local_echo_id or a real event_id."""
        async with self._lock:
            cur = await self.db.execute(
                """
                UPDATE messages SET body = ?, edited = 1
                 WHERE local_echo_id = ? OR event_id = ?
                """,
                (body, local_echo_id, local_echo_id),
            )
            await self.db.commit()
            return cur.rowcount > 0

    async def redact_message(self, ref: str) -> bool:
        """Optimistic delete: ref may be an event_id or a local_echo_id."""
        async with self._lock:
            cur = await self.db.execute(
                """
                UPDATE messages SET redacted = 1, body = ''
                 WHERE event_id = ? OR local_echo_id = ?
                """,
                (ref, ref),
            )
            await self.db.commit()
            return cur.rowcount > 0

    async def upsert_received_message(
        self,
        event_id: str,
        room_id: str,
        sender: str,
        body: str,
        server_ts: float,
    ) -> bool:
        """Insert a message that arrived via sync. Idempotent on event_id.
        Returns True when this is a genuinely new message."""
        async with self._lock:
            cur = await self.db.execute(
                """
                INSERT INTO messages
                    (event_id, local_echo_id, room_id, sender, body, ts,
                     server_ts, status)
                VALUES (?, NULL, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(event_id) DO NOTHING
                """,
                (
                    event_id,
                    room_id,
                    sender,
                    body,
                    server_ts,
                    server_ts,
                    MessageStatus.RECEIVED.value,
                ),
            )
            await self.db.commit()
            return cur.rowcount > 0

    async def apply_server_edit(self, event_id: str, body: str) -> bool:
        async with self._lock:
            cur = await self.db.execute(
                "UPDATE messages SET body = ?, edited = 1 WHERE event_id = ?",
                (body, event_id),
            )
            await self.db.commit()
            return cur.rowcount > 0

    async def apply_server_redaction(self, event_id: str) -> bool:
        async with self._lock:
            cur = await self.db.execute(
                "UPDATE messages SET redacted = 1, body = '' WHERE event_id = ?",
                (event_id,),
            )
            await self.db.commit()
            return cur.rowcount > 0

    async def get_messages(self, room_id: str, limit: int = 100) -> list[dict[str, Any]]:
        """The UI's read path: local only, always fast, never blocks."""
        async with self._lock:
            cur = await self.db.execute(
                """
                SELECT * FROM messages
                 WHERE room_id = ?
                 ORDER BY ts DESC, id DESC
                 LIMIT ?
                """,
                (room_id, limit),
            )
            rows = await cur.fetchall()
        return [_row_to_dict(r) for r in reversed(rows)]

    async def get_message_by_echo(self, local_echo_id: str) -> Optional[dict[str, Any]]:
        async with self._lock:
            cur = await self.db.execute(
                "SELECT * FROM messages WHERE local_echo_id = ?", (local_echo_id,)
            )
            row = await cur.fetchone()
        return _row_to_dict(row) if row else None

    async def get_message_by_event_id(self, event_id: str) -> Optional[dict[str, Any]]:
        async with self._lock:
            cur = await self.db.execute(
                "SELECT * FROM messages WHERE event_id = ?", (event_id,)
            )
            row = await cur.fetchone()
        return _row_to_dict(row) if row else None

    async def pending_messages(self) -> list[dict[str, Any]]:
        """All messages that still owe the server a delivery attempt.
        Used to rebuild the outbox after a restart."""
        async with self._lock:
            cur = await self.db.execute(
                "SELECT * FROM messages WHERE status = ? ORDER BY ts",
                (MessageStatus.PENDING.value,),
            )
            rows = await cur.fetchall()
        return [_row_to_dict(r) for r in rows]

    async def count_by_status(self) -> dict[str, int]:
        async with self._lock:
            cur = await self.db.execute(
                "SELECT status, COUNT(*) AS n FROM messages GROUP BY status"
            )
            rows = await cur.fetchall()
        return {r["status"]: r["n"] for r in rows}

    async def delete_messages_before(self, cutoff_ts: float) -> int:
        """Enterprise retention: hard-delete messages older than the cutoff."""
        async with self._lock:
            cur = await self.db.execute(
                "DELETE FROM messages WHERE ts < ?", (cutoff_ts,)
            )
            await self.db.commit()
            return cur.rowcount

    # -------------------------------------------------------------- sync state

    async def set_sync_token(self, token: str) -> None:
        async with self._lock:
            await self.db.execute(
                """
                INSERT INTO sync_state (key, value) VALUES ('sync_token', ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (token,),
            )
            await self.db.commit()

    async def get_sync_token(self) -> Optional[str]:
        async with self._lock:
            cur = await self.db.execute(
                "SELECT value FROM sync_state WHERE key = 'sync_token'"
            )
            row = await cur.fetchone()
        return row["value"] if row else None

    async def export_state(self) -> dict[str, Any]:
        """Full snapshot, handy for debugging and tests."""
        return {
            "rooms": await self.get_rooms(),
            "messages": {
                room["room_id"]: await self.get_messages(room["room_id"])
                for room in await self.get_rooms()
            },
            "sync_token": await self.get_sync_token(),
        }
