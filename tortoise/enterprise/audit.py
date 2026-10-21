"""Enterprise audit logging.

Compliance is the single strongest reason organizations pay: regulators
ask "who did what, when" and a chat platform must be able to answer.
Every API call can be recorded here in an append-only table that lives
in its own database, separate from message data.
"""
from __future__ import annotations

import json
import time
from typing import Any, Optional

import aiosqlite

SCHEMA = """
CREATE TABLE IF NOT EXISTS audit_events (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    ts       REAL NOT NULL,
    actor    TEXT NOT NULL,
    action   TEXT NOT NULL,
    target   TEXT NOT NULL DEFAULT '',
    meta     TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_events(ts);
CREATE INDEX IF NOT EXISTS idx_audit_actor ON audit_events(actor);
"""


class AuditLogger:
    """Append-only audit trail."""

    def __init__(self, db_path: str = ":memory:") -> None:
        self.db_path = db_path
        self._db: Optional[aiosqlite.Connection] = None

    async def connect(self) -> None:
        if self._db is not None:
            return
        self._db = await aiosqlite.connect(self.db_path)
        self._db.row_factory = aiosqlite.Row
        await self._db.executescript(SCHEMA)
        await self._db.commit()

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    @property
    def db(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("AuditLogger.connect() must be awaited first")
        return self._db

    async def record(
        self, actor: str, action: str, target: str = "", meta: Optional[dict] = None
    ) -> int:
        await self.db.execute(
            "INSERT INTO audit_events (ts, actor, action, target, meta) VALUES (?,?,?,?,?)",
            (time.time(), actor, action, target, json.dumps(meta or {})),
        )
        await self.db.commit()
        cur = await self.db.execute("SELECT last_insert_rowid()")
        row = await cur.fetchone()
        return int(row[0])

    async def query(
        self,
        limit: int = 100,
        actor: Optional[str] = None,
        action_prefix: Optional[str] = None,
        since: Optional[float] = None,
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM audit_events WHERE 1=1"
        params: list[Any] = []
        if actor:
            sql += " AND actor = ?"
            params.append(actor)
        if action_prefix:
            sql += " AND action LIKE ?"
            params.append(f"{action_prefix}%")
        if since:
            sql += " AND ts >= ?"
            params.append(since)
        sql += " ORDER BY ts DESC LIMIT ?"
        params.append(limit)
        cur = await self.db.execute(sql, params)
        rows = await cur.fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["meta"] = json.loads(d["meta"])
            out.append(d)
        return out

    async def count(self) -> int:
        cur = await self.db.execute("SELECT COUNT(*) FROM audit_events")
        row = await cur.fetchone()
        return int(row[0])
