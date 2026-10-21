"""Transport layer: everything that talks to a Matrix homeserver.

Two implementations:

* :class:`MockMatrixServer` - an in-memory homeserver with *injectable
  latency*. This is the workhorse of the demo and the test-suite: it lets
  us prove that a pathologically slow server cannot degrade the client.
* :class:`RealMatrixTransport` - a thin httpx client speaking the Matrix
  Client-Server API (v3 subset). Experimental, but real.

Both raise :class:`RetryableError` for transient failures and
:class:`PermanentError` for requests the server refuses for good.
"""
from __future__ import annotations

import asyncio
import json
import random
import time
import uuid
from abc import ABC, abstractmethod
from typing import Any, Optional

from .queue import PermanentError, RetryableError


class Transport(ABC):
    """Interface every homeserver adapter implements."""

    @abstractmethod
    async def send_message(self, room_id: str, sender: str, body: str) -> dict[str, Any]:
        """Returns ``{"event_id": str, "ts": float}``."""

    @abstractmethod
    async def edit_message(self, room_id: str, event_id: str, body: str) -> dict[str, Any]:
        ...

    @abstractmethod
    async def redact_message(self, room_id: str, event_id: str) -> dict[str, Any]:
        ...

    @abstractmethod
    async def join_room(self, room_id: str, name: str = "") -> dict[str, Any]:
        ...

    @abstractmethod
    async def leave_room(self, room_id: str) -> dict[str, Any]:
        ...

    @abstractmethod
    async def set_room_notifications(
        self, room_id: str, prefs: dict[str, Any]
    ) -> dict[str, Any]:
        ...

    @abstractmethod
    async def sync(self, since: Optional[str] = None, timeout: int = 30) -> dict[str, Any]:
        """Incremental sync. Returns ``{"next_batch": str, "events": [...],
        "rooms": [...], "limited": bool}``."""


# ---------------------------------------------------------------------------
# Mock homeserver
# ---------------------------------------------------------------------------


class MockMatrixServer(Transport):
    """An in-memory homeserver with injectable latency and failure rates.

    Every call sleeps for ``latency +/- jitter`` seconds before answering,
    which is exactly what a slow, overloaded, or mobile-network server
    feels like from the client's perspective.
    """

    def __init__(
        self,
        latency: float = 2.5,
        jitter: float = 1.0,
        failure_rate: float = 0.0,
        sync_limit: int = 50,
        seed: Optional[int] = None,
    ) -> None:
        self.latency = latency
        self.jitter = jitter
        self.failure_rate = failure_rate
        self.sync_limit = sync_limit
        self._rng = random.Random(seed)
        self._lock = asyncio.Lock()
        self._seq = 0
        self._next_batch = 0
        self.rooms: dict[str, dict[str, Any]] = {}
        self.events: list[dict[str, Any]] = []
        self.call_log: list[tuple[float, str, str]] = []  # (ts, method, room)

    # ------------------------------------------------------------ internals

    async def _network_delay(self) -> None:
        """Simulate the wire *outside* the state lock, so concurrent requests
        overlap the way they would on a real homeserver."""
        await asyncio.sleep(self._delay())
        if self.failure_rate and self._rng.random() < self.failure_rate:
            raise RetryableError("simulated network failure")

    def _delay(self) -> float:
        return max(0.0, self._rng.uniform(self.latency - self.jitter, self.latency + self.jitter))

    def _new_event_id(self) -> str:
        return f"${uuid.uuid4().hex}"

    def _append_event(self, event: dict[str, Any]) -> dict[str, Any]:
        self._seq += 1
        self._next_batch += 1
        event = {**event, "seq": self._seq}
        self.events.append(event)
        return event

    # -------------------------------------------------------------- mutations

    async def send_message(self, room_id: str, sender: str, body: str) -> dict[str, Any]:
        await self._network_delay()
        async with self._lock:
            if room_id not in self.rooms:
                raise PermanentError(f"unknown room {room_id}")
            event = self._append_event(
                {
                    "event_id": self._new_event_id(),
                    "type": "m.room.message",
                    "room_id": room_id,
                    "sender": sender,
                    "body": body,
                    "ts": time.time(),
                }
            )
            self.call_log.append((time.time(), "send", room_id))
            return {"event_id": event["event_id"], "ts": event["ts"]}

    async def edit_message(self, room_id: str, event_id: str, body: str) -> dict[str, Any]:
        await self._network_delay()
        async with self._lock:
            target = next((e for e in self.events if e["event_id"] == event_id), None)
            if target is None:
                raise PermanentError(f"unknown event {event_id}")
            self._append_event(
                {
                    "event_id": self._new_event_id(),
                    "type": "m.room.message.edit",
                    "room_id": room_id,
                    "sender": target["sender"],
                    "body": body,
                    "ts": time.time(),
                    "target_event_id": event_id,
                }
            )
            self.call_log.append((time.time(), "edit", room_id))
            return {"event_id": event_id, "ts": time.time()}

    async def redact_message(self, room_id: str, event_id: str) -> dict[str, Any]:
        await self._network_delay()
        async with self._lock:
            target = next((e for e in self.events if e["event_id"] == event_id), None)
            if target is None:
                raise PermanentError(f"unknown event {event_id}")
            target["redacted"] = True
            self._append_event(
                {
                    "event_id": self._new_event_id(),
                    "type": "m.room.redaction",
                    "room_id": room_id,
                    "sender": target["sender"],
                    "ts": time.time(),
                    "target_event_id": event_id,
                }
            )
            self.call_log.append((time.time(), "redact", room_id))
            return {"event_id": event_id, "ts": time.time()}

    async def join_room(self, room_id: str, name: str = "") -> dict[str, Any]:
        await self._network_delay()
        async with self._lock:
            self.rooms.setdefault(room_id, {"name": name or room_id, "joined": True})
            self.call_log.append((time.time(), "join", room_id))
            return {"room_id": room_id, "name": self.rooms[room_id]["name"]}

    async def leave_room(self, room_id: str) -> dict[str, Any]:
        await self._network_delay()
        async with self._lock:
            if room_id in self.rooms:
                self.rooms[room_id]["joined"] = False
            self.call_log.append((time.time(), "leave", room_id))
            return {"room_id": room_id}

    async def set_room_notifications(
        self, room_id: str, prefs: dict[str, Any]
    ) -> dict[str, Any]:
        await self._network_delay()
        async with self._lock:
            room = self.rooms.setdefault(room_id, {"name": room_id, "joined": True})
            room["notifications"] = prefs
            self.call_log.append((time.time(), "prefs", room_id))
            return {"room_id": room_id, "notifications": prefs}

    # ------------------------------------------------------------------ sync

    async def sync(self, since: Optional[str] = None, timeout: int = 30) -> dict[str, Any]:
        await self._network_delay()
        async with self._lock:
            since_seq = int(since) if since else 0
            fresh = [e for e in self.events if e["seq"] > since_seq]
            limited = len(fresh) > self.sync_limit
            if limited:
                fresh = fresh[: self.sync_limit]
            self.call_log.append((time.time(), "sync", ""))
            # When the response is limited, next_batch must point at the last
            # event we actually returned - not at the server's newest seq -
            # otherwise the tail of the burst is lost forever.
            next_batch = fresh[-1]["seq"] if fresh else self._next_batch
            return {
                "next_batch": str(next_batch),
                "events": [self._public_view(e) for e in fresh],
                "rooms": [
                    {"room_id": rid, **meta} for rid, meta in self.rooms.items()
                ],
                "limited": limited,
            }

    def _public_view(self, event: dict[str, Any]) -> dict[str, Any]:
        """What a real /sync timeline event looks like, minus private fields."""
        view = {k: v for k, v in event.items() if k != "seq"}
        if view.get("type") == "m.room.message.edit":
            view = {
                "event_id": view["event_id"],
                "type": "m.room.message.edit",
                "room_id": view["room_id"],
                "sender": view["sender"],
                "ts": view["ts"],
                "target_event_id": view["target_event_id"],
                "body": view["body"],
            }
        if view.get("type") == "m.room.redaction":
            view = {
                "event_id": view["event_id"],
                "type": "m.room.redaction",
                "room_id": view["room_id"],
                "sender": view["sender"],
                "ts": view["ts"],
                "target_event_id": view["target_event_id"],
            }
        return view

    # ------------------------------------------------------- demo/test hooks

    async def peer_says(
        self, room_id: str, sender: str, body: str, after: float = 1.0
    ) -> dict[str, Any]:
        """Simulate *another user* sending a message (demo NPCs)."""
        await asyncio.sleep(after)
        async with self._lock:
            if room_id not in self.rooms:
                raise PermanentError(f"unknown room {room_id}")
            event = self._append_event(
                {
                    "event_id": self._new_event_id(),
                    "type": "m.room.message",
                    "room_id": room_id,
                    "sender": sender,
                    "body": body,
                    "ts": time.time(),
                }
            )
            return self._public_view(event)

    def stats(self) -> dict[str, Any]:
        return {
            "latency": self.latency,
            "jitter": self.jitter,
            "failure_rate": self.failure_rate,
            "calls": len(self.call_log),
            "events": len(self.events),
            "rooms": len(self.rooms),
        }


# ---------------------------------------------------------------------------
# Real homeserver (Matrix Client-Server API v3 subset)
# ---------------------------------------------------------------------------


class RealMatrixTransport(Transport):
    """httpx client for a real Matrix homeserver. Experimental.

    Covers the subset Tortoise needs: login, sync, message send/edit/redact,
    room join/leave and notification preferences. Good enough to run against
    a local Synapse/Conduit/Dendrite for dogfooding.
    """

    def __init__(self, homeserver: str, access_token: Optional[str] = None) -> None:
        self.homeserver = homeserver.rstrip("/")
        self.access_token = access_token
        self._client = None

    async def __aenter__(self) -> "RealMatrixTransport":
        import httpx

        self._client = httpx.AsyncClient(base_url=self.homeserver, timeout=60.0)
        return self

    async def __aexit__(self, *exc: Any) -> None:
        if self._client is not None:
            await self._client.aclose()

    @property
    def client(self):
        if self._client is None:
            raise RuntimeError("use `async with RealMatrixTransport(...)`")
        return self._client

    async def login(self, username: str, password: str) -> str:
        resp = await self.client.post(
            "/_matrix/client/v3/login",
            json={
                "type": "m.login.password",
                "identifier": {"type": "m.id.user", "user": username},
                "password": password,
            },
        )
        resp.raise_for_status()
        self.access_token = resp.json()["access_token"]
        return self.access_token

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.access_token}"}

    async def send_message(self, room_id: str, sender: str, body: str) -> dict[str, Any]:
        txn = uuid.uuid4().hex
        resp = await self.client.put(
            f"/_matrix/client/v3/rooms/{room_id}/send/m.room.message/{txn}",
            headers=self._headers(),
            json={"msgtype": "m.text", "body": body},
        )
        if resp.status_code >= 500:
            raise RetryableError(f"server error {resp.status_code}")
        if resp.status_code >= 400:
            raise PermanentError(f"rejected: {resp.text}")
        return {"event_id": resp.json()["event_id"], "ts": time.time()}

    async def edit_message(self, room_id: str, event_id: str, body: str) -> dict[str, Any]:
        txn = uuid.uuid4().hex
        resp = await self.client.put(
            f"/_matrix/client/v3/rooms/{room_id}/send/m.room.message/{txn}",
            headers=self._headers(),
            json={
                "msgtype": "m.text",
                "body": f"* {body}",
                "m.new_content": {"msgtype": "m.text", "body": body},
                "m.relates_to": {"rel_type": "m.replace", "event_id": event_id},
            },
        )
        if resp.status_code >= 500:
            raise RetryableError(f"server error {resp.status_code}")
        if resp.status_code >= 400:
            raise PermanentError(f"rejected: {resp.text}")
        return {"event_id": event_id, "ts": time.time()}

    async def redact_message(self, room_id: str, event_id: str) -> dict[str, Any]:
        txn = uuid.uuid4().hex
        resp = await self.client.put(
            f"/_matrix/client/v3/rooms/{room_id}/send/m.room.redaction/{txn}",
            headers=self._headers(),
            json={},
        )
        if resp.status_code >= 500:
            raise RetryableError(f"server error {resp.status_code}")
        if resp.status_code >= 400:
            raise PermanentError(f"rejected: {resp.text}")
        return {"event_id": event_id, "ts": time.time()}

    async def join_room(self, room_id: str, name: str = "") -> dict[str, Any]:
        resp = await self.client.post(
            f"/_matrix/client/v3/rooms/{room_id}/join", headers=self._headers(), json={}
        )
        if resp.status_code >= 500:
            raise RetryableError(f"server error {resp.status_code}")
        if resp.status_code >= 400:
            raise PermanentError(f"rejected: {resp.text}")
        return {"room_id": room_id, "name": name or room_id}

    async def leave_room(self, room_id: str) -> dict[str, Any]:
        resp = await self.client.post(
            f"/_matrix/client/v3/rooms/{room_id}/leave", headers=self._headers(), json={}
        )
        if resp.status_code >= 500:
            raise RetryableError(f"server error {resp.status_code}")
        if resp.status_code >= 400:
            raise PermanentError(f"rejected: {resp.text}")
        return {"room_id": room_id}

    async def set_room_notifications(
        self, room_id: str, prefs: dict[str, Any]
    ) -> dict[str, Any]:
        # Real homeservers vary here; PUT to the room account data is the
        # common denominator. Marked experimental in the docs.
        resp = await self.client.put(
            f"/_matrix/client/v3/user/{self._me()}/rooms/{room_id}/account_data/m.notifications",
            headers=self._headers(),
            json=prefs,
        )
        if resp.status_code >= 500:
            raise RetryableError(f"server error {resp.status_code}")
        if resp.status_code >= 400:
            raise PermanentError(f"rejected: {resp.text}")
        return {"room_id": room_id, "notifications": prefs}

    def _me(self) -> str:
        return "@tortoise:localhost"

    async def sync(self, since: Optional[str] = None, timeout: int = 30) -> dict[str, Any]:
        params: dict[str, Any] = {"timeout": timeout * 1000}
        if since:
            params["since"] = since
        resp = await self.client.get(
            "/_matrix/client/v3/sync", headers=self._headers(), params=params
        )
        if resp.status_code >= 500:
            raise RetryableError(f"server error {resp.status_code}")
        resp.raise_for_status()
        data = resp.json()
        events: list[dict[str, Any]] = []
        rooms_out: list[dict[str, Any]] = []
        for room_id, room in data.get("rooms", {}).get("join", {}).items():
            rooms_out.append(
                {"room_id": room_id, "name": room.get("name", room_id), "joined": True}
            )
            for ev in room.get("timeline", {}).get("events", []):
                if ev.get("type") == "m.room.message":
                    events.append(
                        {
                            "event_id": ev["event_id"],
                            "type": "m.room.message",
                            "room_id": room_id,
                            "sender": ev["sender"],
                            "body": ev.get("content", {}).get("body", ""),
                            "ts": time.time(),
                        }
                    )
        return {
            "next_batch": data.get("next_batch", since or ""),
            "events": events,
            "rooms": rooms_out,
            "limited": False,
        }
