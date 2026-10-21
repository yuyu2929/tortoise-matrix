"""Interactive demo application.

The demo exists to make one point viscerally: *a slow server must not
degrade the client*. Two chat UIs run side by side against the **same**
mock homeserver:

* **Legacy client (top)** - the traditional blocking design. Every action
  waits for the server before the UI reacts. Its latency is real: the
  endpoints hold the request until the mock server answers.
* **Tortoise client (bottom)** - this project. Actions return instantly,
  acknowledgements arrive over the websocket, and incoming messages flow
  in via background sync.

A latency slider retargets the shared mock server live, so you can watch
both panels degrade (or not) as the server gets slower.
"""
from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from pydantic import BaseModel

from ..client import TortoiseClient
from ..core.transport import MockMatrixServer
from ..server import create_app

STATIC_DIR = Path(__file__).resolve().parent.parent.parent / "static"

DEMO_ROOMS = [
    ("!design:localhost", "产品设计"),
    ("!random:localhost", "随便聊聊"),
]

NPC_LINES = [
    ("!design:localhost", "@alice:localhost", "原型我又改了一版，圆形头像确实好看"),
    ("!random:localhost", "@bob:localhost", "刚才谁发的消息我这边才收到，服务器抽风了"),
    ("!design:localhost", "@carol:localhost", "通知设置我改成仅提及时提醒了"),
    ("!random:localhost", "@alice:localhost", "今晚加班吗？不同步一下"),
]

NPC_INTERVAL = 14.0


class LegacySendBody(BaseModel):
    room_id: str
    body: str


def create_demo_app(
    latency: float = 2.5,
    jitter: float = 1.0,
    failure_rate: float = 0.0,
) -> FastAPI:
    server = MockMatrixServer(latency=latency, jitter=jitter, failure_rate=failure_rate)
    client = TortoiseClient(
        transport=server, db_path=":memory:", local_user="@me:localhost"
    )
    npc_task: dict[str, asyncio.Task] = {}

    async def _npc_loop() -> None:
        i = 0
        while True:
            await asyncio.sleep(NPC_INTERVAL)
            room, sender, line = NPC_LINES[i % len(NPC_LINES)]
            i += 1
            try:
                await server.peer_says(room, sender, line, after=0.0)
            except Exception:
                pass

    @asynccontextmanager
    async def _lifespan(app: FastAPI):
        await client.start()
        for room_id, name in DEMO_ROOMS:
            # Register the demo rooms on the mock *server* synchronously
            # first: the legacy comparison endpoints and the NPC loop talk
            # to the server directly and must never race the optimistic
            # join (which only lands on the server after a full round-trip).
            try:
                await server.join_room(room_id, name=name)
            except Exception:
                pass
            await client.join_room(room_id, name=name)
        npc_task["task"] = asyncio.create_task(_npc_loop())
        try:
            yield
        finally:
            npc_task["task"].cancel()
            await client.stop()

    app = create_app(
        client,
        static_dir=None,  # mounted last, after all API routes are registered
        title="Tortoise Matrix Demo",
        lifespan=_lifespan,
    )

    # ------------------------------------------------------- legacy endpoints
    # These intentionally BLOCK until the mock server answers - they are the
    # "traditional client" the demo compares against.

    @app.post("/api/demo/legacy/send")
    async def legacy_send(body: LegacySendBody) -> dict[str, Any]:
        t0 = time.perf_counter()
        resp = await server.send_message(body.room_id, "@me:localhost", body.body)
        blocked = time.perf_counter() - t0
        return {"event_id": resp["event_id"], "blocked_seconds": round(blocked, 3)}

    @app.get("/api/demo/legacy/messages")
    async def legacy_messages(room_id: str) -> dict[str, Any]:
        # The legacy client re-reads from the server on every refresh.
        await asyncio.sleep(server.latency)
        messages = await client.get_messages(room_id)
        return {"messages": messages, "waited": round(server.latency, 3)}

    @app.get("/api/demo/config")
    async def demo_config() -> dict[str, Any]:
        return {
            "latency": server.latency,
            "jitter": server.jitter,
            "failure_rate": server.failure_rate,
            "rooms": [{"room_id": r, "name": n} for r, n in DEMO_ROOMS],
            "local_user": client.local_user,
        }

    # Static files mount LAST: a mount on "/" would otherwise shadow any
    # API route registered after it.
    if STATIC_DIR.is_dir():
        from fastapi.staticfiles import StaticFiles

        app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")

    return app


def main() -> None:
    import uvicorn

    uvicorn.run(
        create_demo_app(),
        host="127.0.0.1",
        port=8765,
        log_level="info",
    )


if __name__ == "__main__":
    main()
