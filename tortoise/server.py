"""FastAPI wrapper around the Tortoise client.

Exposes the client's guarantees over HTTP:

* ``POST`` (mutating) endpoints return the **local** view immediately -
  the response body is the optimistic event, not a server round-trip.
* ``GET`` (reading) endpoints are pure local reads - microseconds.
* ``GET /ws`` streams every engine event (optimistic updates,
  acknowledgements, sync state, queue metrics) to connected UIs.

Enterprise deployments additionally get the admin router
(``tortoise.enterprise.admin``) and request auditing.
"""
from __future__ import annotations

import json
from typing import Any, Optional

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .client import TortoiseClient
from .core.events import TortoiseEvent


# --------------------------------------------------------------------- models


class SendMessageBody(BaseModel):
    body: str = Field(min_length=1, max_length=10000)
    sender: Optional[str] = None


class EditMessageBody(BaseModel):
    body: str = Field(min_length=1, max_length=10000)


class JoinRoomBody(BaseModel):
    name: str = ""
    room_id: Optional[str] = None


class NotificationsBody(BaseModel):
    prefs: dict[str, Any]


class SetLatencyBody(BaseModel):
    latency: float = Field(ge=0, le=60)
    jitter: float = Field(default=1.0, ge=0, le=60)
    failure_rate: float = Field(default=0.0, ge=0, le=1)


class PeerMessageBody(BaseModel):
    room_id: str
    sender: str
    body: str
    after: float = 1.0


# ---------------------------------------------------------------------- helpers


def _event_payload(event: TortoiseEvent) -> dict[str, Any]:
    """Serialize any TortoiseEvent to JSON for the websocket."""
    out: dict[str, Any] = {"kind": event.kind, "seq": event.seq, "ts": event.ts}
    for f in event.__dataclass_fields__:
        if f in ("seq", "ts"):
            continue
        val = getattr(event, f)
        if hasattr(val, "value"):  # enums
            val = val.value
        out[f] = val
    return out


# ------------------------------------------------------------------------ app


def create_app(
    client: TortoiseClient,
    enterprise: Optional[Any] = None,
    static_dir: Optional[str] = None,
    title: str = "Tortoise Matrix API",
    lifespan: Optional[Any] = None,
) -> FastAPI:
    """Build the FastAPI app around a started TortoiseClient.

    ``enterprise`` may be an :class:`tortoise.enterprise.EnterpriseManager`
    enabling the admin router and request auditing.
    """
    app = FastAPI(
        title=title,
        version="0.1.0",
        description=(
            "Slow-friendly Matrix client API. Mutations return the local "
            "view instantly; reads never touch the network."
        ),
        lifespan=lifespan,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.state.client = client
    app.state.enterprise = enterprise

    # ------------------------------------------------------------- audit hook

    if enterprise is not None and getattr(enterprise, "audit", None) is not None:
        @app.middleware("http")
        async def _audit(request, call_next):
            response = await call_next(request)
            try:
                await enterprise.audit.record(
                    actor=request.headers.get("x-tortoise-actor", "anonymous"),
                    action=f"{request.method} {request.url.path}",
                    target=request.url.path,
                    meta={"status": response.status_code},
                )
            except Exception:
                pass
            return response

    # ---------------------------------------------------------------- status

    @app.get("/api/status")
    async def get_status() -> dict[str, Any]:
        status = await client.status()
        status["enterprise"] = bool(enterprise is not None)
        return status

    # ----------------------------------------------------------------- rooms

    @app.get("/api/rooms")
    async def list_rooms() -> list[dict[str, Any]]:
        return await client.get_rooms()

    @app.post("/api/rooms/{room_id}/join")
    async def join_room(room_id: str, body: JoinRoomBody) -> dict[str, Any]:
        rid = body.room_id or room_id
        event = await client.join_room(rid, name=body.name)
        return _event_payload(event)

    @app.post("/api/rooms/{room_id}/leave")
    async def leave_room(room_id: str) -> dict[str, Any]:
        event = await client.leave_room(room_id)
        return _event_payload(event)

    @app.get("/api/rooms/{room_id}/messages")
    async def list_messages(room_id: str, limit: int = 100) -> list[dict[str, Any]]:
        return await client.get_messages(room_id, limit)

    @app.post("/api/rooms/{room_id}/messages", status_code=201)
    async def send_message(room_id: str, body: SendMessageBody) -> dict[str, Any]:
        sender = body.sender or client.local_user
        event = await client.send_message(room_id, sender, body.body)
        return _event_payload(event)

    @app.patch("/api/rooms/{room_id}/messages/{ref}")
    async def edit_message(room_id: str, ref: str, body: EditMessageBody) -> dict[str, Any]:
        try:
            event = await client.edit_message(room_id, ref, body.body)
        except KeyError:
            raise HTTPException(404, f"message {ref!r} not found")
        return _event_payload(event)

    @app.delete("/api/rooms/{room_id}/messages/{ref}")
    async def delete_message(room_id: str, ref: str) -> dict[str, Any]:
        await client.delete_message(room_id, ref)
        return {"deleted": ref}

    @app.post("/api/rooms/{room_id}/read")
    async def mark_read(room_id: str) -> dict[str, Any]:
        await client.mark_read(room_id)
        return {"room_id": room_id, "unread": 0}

    @app.post("/api/rooms/{room_id}/notifications")
    async def set_notifications(room_id: str, body: NotificationsBody) -> dict[str, Any]:
        event = await client.set_room_notifications(room_id, body.prefs)
        return _event_payload(event)

    # ------------------------------------------------------------ demo hooks

    @app.post("/api/demo/latency")
    async def set_latency(body: SetLatencyBody) -> dict[str, Any]:
        """Adjust the mock server's latency live (demo only)."""
        transport = client.transport
        if not hasattr(transport, "latency"):
            raise HTTPException(400, "transport does not support latency injection")
        transport.latency = body.latency
        transport.jitter = body.jitter
        transport.failure_rate = body.failure_rate
        return {
            "latency": transport.latency,
            "jitter": transport.jitter,
            "failure_rate": transport.failure_rate,
        }

    @app.post("/api/demo/peer")
    async def peer_message(body: PeerMessageBody) -> dict[str, Any]:
        """Simulate another user sending a message (demo NPC)."""
        transport = client.transport
        if not hasattr(transport, "peer_says"):
            raise HTTPException(400, "transport does not support peer messages")
        event = await transport.peer_says(
            body.room_id, body.sender, body.body, after=body.after
        )
        return event

    # ------------------------------------------------------------ websocket

    @app.websocket("/ws")
    async def websocket(ws: WebSocket) -> None:
        await ws.accept()

        async def _push(event: TortoiseEvent) -> None:
            try:
                await ws.send_text(json.dumps(_event_payload(event)))
            except Exception:
                pass

        client.subscribe("*", _push)
        try:
            # Keep the socket open; the client may send pings.
            while True:
                await ws.receive_text()
        except WebSocketDisconnect:
            pass
        finally:
            client.unsubscribe("*", _push)

    # ------------------------------------------------------------ enterprise

    if enterprise is not None and getattr(enterprise, "admin_router", None) is not None:
        app.include_router(enterprise.admin_router)

    # --------------------------------------------------------------- static
    # NOTE: if you register more routes after calling create_app(), mount
    # static files yourself afterwards - a mount on "/" shadows later routes.

    if static_dir:
        try:
            app.mount("/", StaticFiles(directory=static_dir, html=True), name="static")
        except Exception:
            pass

    return app
