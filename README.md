<p align="center"><b>Tortoise Matrix</b></p>
<h3 align="center">A slow-friendly Matrix chat client</h3>
<p align="center"><i>Every server-dependent operation is non-blocking.<br>A slow homeserver can no longer slow you down.</i></p>
<p align="center">
  <a href="#quick-start">Quick start</a> ·
  <a href="#http-api">API</a> ·
  <a href="./ARCHITECTURE.md">Architecture</a> ·
  <a href="./COMMERCIAL.md">Licensing</a>
</p>
<p align="center">
  <a href="https://github.com/yuyu2929/tortoise-matrix/actions/workflows/ci.yml"><img src="https://github.com/yuyu2929/tortoise-matrix/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <img src="https://img.shields.io/badge/license-AGPL--3.0%20%2B%20commercial-blue" alt="License: AGPL-3.0 + commercial">
  <img src="https://img.shields.io/badge/python-3.10%7C3.11%7C3.12%7C3.13-informational" alt="Python 3.10+">
</p>

---

Implements the idea from [open-source-ideas/ideas#430](https://github.com/open-source-ideas/ideas/issues/430): a Matrix client where **syncing, joining/leaving rooms, accepting chat requests, editing notification preferences, sending/editing/deleting messages** — everything that depends on a backend — happens in a way that never degrades the user experience, no matter how slow the server is.

**Free for personal use** (AGPL-3.0). **Organizations license the enterprise tier** — see [COMMERCIAL.md](./COMMERCIAL.md).

## The idea in one picture

```
        Traditional client                     Tortoise
   ┌──────────────────────────┐      ┌──────────────────────────┐
   │  UI ← waits → server     │      │  UI ← local store (µs)   │
   │  (server slowness =      │      │        ↑ writes first    │
   │   frozen interface)      │      │  queue → server (async)  │
   └──────────────────────────┘      │  acks reconcile quietly  │
                                     └──────────────────────────┘
```

Measured against a mock homeserver with **2.5 s latency**:

| Operation | Traditional | Tortoise |
|---|---|---|
| Send a message (until visible) | ~2 500 ms | **< 1 ms** |
| Read the room | ~2 500 ms | **< 1 ms** |
| Incoming message while offline | lost until manual refresh | **arrives via background sync** |

## How it works

Five small modules, one invariant: **the UI only ever talks to the local store; the network is somebody else's problem.**

| Module | Role |
|---|---|
| `core/localstore.py` | SQLite store — the single source of truth the UI reads from. Microsecond reads, WAL mode. |
| `core/optimistic.py` | Turns user actions into instant local events (clock icon) plus background jobs (checkmark on ack). |
| `core/queue.py` | Priority queue for all network work: user actions never dropped, exponential-backoff retries forever, coalescing and backpressure for background tasks. |
| `core/sync.py` | Background incremental sync with gap catch-up. Reports `syncing / live / lagging`; a slow server changes nothing for the UI. |
| `core/transport.py` | Pluggable transports: `MockMatrixServer` (injectable latency — the demo and test workhorse) and `RealMatrixTransport` (httpx, Matrix Client-Server API v3 subset). |

Deep dive: [ARCHITECTURE.md](./ARCHITECTURE.md).

## Quick start

Requires Python 3.10+.

```bash
git clone https://github.com/yuyu2929/tortoise-matrix.git
cd tortoise-matrix
pip install -e ".[dev]"

# Run the side-by-side demo (legacy blocking client vs Tortoise,
# same mock server, live latency slider):
tortoise-demo
# → open http://127.0.0.1:8765
```

Then drag the **server latency** slider up and watch the traditional client freeze while Tortoise keeps rendering instantly.

### Use it as a library

```python
import asyncio
from tortoise import TortoiseClient, MockMatrixServer

async def main():
    async with TortoiseClient(
        transport=MockMatrixServer(latency=3.0),   # a horrible server
        local_user="@me:localhost",
    ) as c:
        await c.join_room("!room:localhost", name="Slow Server Stress Test")

        event = await c.send_message("!room:localhost", "@me:localhost", "hello")
        print(event.status)          # "pending"  — visible ALREADY
        print(await c.get_messages("!room:localhost"))  # instant, local

asyncio.run(main())
```

Or serve it as an API:

```python
from tortoise import TortoiseClient
from tortoise.server import create_app

client = TortoiseClient()          # wire your transport of choice
app = create_app(client)           # FastAPI: REST + websocket event stream
```

## HTTP API

| Method | Path | Blocks? |
|---|---|---|
| `GET` | `/api/rooms` | never (local) |
| `POST` | `/api/rooms/{id}/join` · `/leave` | never (optimistic) |
| `GET` | `/api/rooms/{id}/messages` | never (local) |
| `POST` | `/api/rooms/{id}/messages` | never (optimistic, `201` + pending event) |
| `PATCH` | `/api/rooms/{id}/messages/{ref}` | never (optimistic edit) |
| `DELETE` | `/api/rooms/{id}/messages/{ref}` | never (optimistic redact) |
| `POST` | `/api/rooms/{id}/notifications` | never (optimistic) |
| `GET` | `/api/status` | never (queue/sync health) |
| `WS` | `/ws` | streams every engine event |

Every mutating response is the **local** event — the caller never waits for a server round-trip. The websocket delivers the full lifecycle: `MessageUpserted` (pending) → `MessageAcknowledged` (sent) / `MessageFailed` (retrying) / `MessageRejected`.

## Free for people, paid for organizations

The same core ships under a dual license:

| | Community (free) | Enterprise (paid) |
|---|---|---|
| Full client, local-first engine, REST/WS API | ✔ | ✔ |
| License | AGPL-3.0 | Commercial |
| Audit trail of every operation | — | ✔ |
| Retention policies & automated purge | — | ✔ |
| Admin API & usage reporting | — | ✔ |
| SSO/SCIM readiness, white-labeling | — | ✔ |
| Support | community | SLA |

Rationale and pricing: [COMMERCIAL.md](./COMMERCIAL.md). Enterprise features are enforced in code (`tortoise/enterprise/`, signed license keys) — the gate is real, not a README promise.

## Tests

```bash
pytest tests/ -q
```

62 tests, including the headline suite `tests/test_slow_server.py`: with a 2-second-latency, packet-losing server, sends stay sub-100 ms, reads stay sub-millisecond, retries eventually land, and permanent rejections are surfaced honestly.

## Status

Alpha. The mock server is fully exercised; `RealMatrixTransport` covers the v3 subset (login, sync, send/edit/redact, join/leave, account data) and is next to mature against a live Synapse. Contributions welcome — see [CONTRIBUTING.md](./CONTRIBUTING.md).

## License

Community edition: AGPL-3.0-or-later (full text in [LICENSE](./LICENSE)). Organizations: commercial license (see [COMMERCIAL.md](./COMMERCIAL.md)).

**一言以蔽之（中文版）**：个人使用、学习、修改、分发**永久免费**；企业/组织在生产环境使用需购买商业许可，费用对应的是审计日志、数据保留策略、管理后台这些合规刚需与 SLA 支持——聊天功能本身没有任何付费墙。
