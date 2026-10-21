# Architecture

This document explains the design of Tortoise: a Matrix client built
around one invariant.

> **The UI only ever reads from the local store. The network is somebody
> else's problem.**

## 1. The layers

```
┌────────────────────────────────────────────────────────┐
│ UI / API consumers                                     │
│  reads  → LocalStore (µs, always)                      │
│  writes → OptimisticEngine → local event, instantly    │
├────────────────────────────────────────────────────────┤
│ LocalStore (SQLite, WAL)                               │
│  rooms · messages (pending/sent/failed/rejected/       │
│  received) · sync cursor · notification prefs          │
├────────────────────────────────────────────────────────┤
│ OptimisticEngine                                       │
│  local echo → emit UI event → enqueue delivery job     │
│  ack  → reconcile echo with real event_id              │
│  fail → mark failed, keep retrying                     │
├────────────────────────────────────────────────────────┤
│ RequestQueue (asyncio, priority)                       │
│  user tasks: never dropped, retry forever              │
│  background tasks: coalesced, dropped under pressure   │
├────────────────────────────────────────────────────────┤
│ SyncEngine (background)                                │
│  incremental sync → apply → emit → advance cursor      │
│  gap catch-up on `limited` responses                   │
├────────────────────────────────────────────────────────┤
│ Transport                                              │
│  MockMatrixServer (injectable latency, demo/tests)     │
│  RealMatrixTransport (Matrix CS API v3, experimental)  │
└────────────────────────────────────────────────────────┘
```

Everything is wired together by a synchronous `EventBus`. Events —
`MessageUpserted`, `MessageAcknowledged`, `MessageFailed`,
`MessageRejected`, `RoomUpserted`, `SyncStateChanged`, `QueueMetrics` —
are the *only* way state changes reach the UI. Emitting never awaits;
a slow subscriber cannot slow the engine.

## 2. The optimistic lifecycle

Sending a message:

1. `OptimisticEngine.send_message()` generates a `local_echo_id`, writes
   a `pending` row into SQLite, emits `MessageUpserted`, **returns**.
   The caller — a GUI, a REST handler — has the message on screen in
   microseconds. No network has been touched.
2. The delivery job enters the request queue. Workers call the transport;
   on success the echo is reconciled with the real `event_id`
   (`MessageAcknowledged`), on retryable failure the row is marked
   `failed` and the job is re-queued with backoff (`MessageFailed`),
   on permanent refusal it is marked `rejected` (`MessageRejected`).

Editing/deleting work the same way — local first. An edit to a message
whose original send is still in flight waits (in the background worker)
for the ack, because the same queue key serializes operations per message.
Deleting a still-pending message cancels its delivery entirely: it never
reaches the server.

A restart is handled by `rebuild_outbox()`: every row still in `pending`
re-enters the queue. Nothing the user sent is ever silently lost.

## 3. The queue contract

`RequestQueue` encodes the product philosophy:

| Rule | Mechanism |
|---|---|
| User actions always win | priority 0; never dropped; retried forever (1→2→4→…→60 s cap) |
| Background work is expendable | dropped past the high-water mark; coalesced when equivalent work is in flight |
| Nothing blocks | enqueue returns immediately; N independent workers |
| Failure is data, not exception | every transition emits an event the UI can render |

Retries distinguish `RetryableError` (timeouts, 5xx, packet loss) from
`PermanentError` (unknown room, malformed request). Infinite retry of the
former is intentional: the queue is fighting *for* the user, quietly.

## 4. Sync with slow-server tolerance

`SyncEngine` polls `/sync` (or the transport equivalent) in a loop:

- Each round goes through the queue at background priority, coalesced, so
  it can be dropped under pressure and never overlaps itself.
- Responses are applied to the store (new messages bump unread for remote
  senders; edits and redactions are applied by `target_event_id`).
- A `limited` response means a burst exceeded one round's capacity; the
  next round fires immediately and the cursor is advanced only to the
  last event actually returned — a tail is never lost.
- Failures flip the state to `lagging`, back off, and try again. The UI
  never learns about a slow server except through a state badge.

## 5. The mock server as a specification

`MockMatrixServer` is not a test stub — it is the executable
specification of "a slow homeserver": injectable latency, jitter and
failure rate, realistic per-message `event_id`s, edit/redaction events,
long-burst `limited` responses. Every guarantee in this document is
demonstrated against it in `tests/test_slow_server.py`, including the
worst cases (60 % packet loss with a 2 s server) where messages stay
queued, keep retrying, and land the moment the server recovers.

## 6. Enterprise features

`EnterpriseManager` composes the paid tier: a signed license key
(`TORT-ENT.<payload>.<hmac>`) gates audit logging (append-only, separate
DB), retention policies (scheduled purge), and the admin API (FastAPI
router returning HTTP 402 without a valid license). The gate is enforced
in code, not by trust. Production deployments should replace the demo
signing secret with a server-side verification service.

## 7. Why AGPL for the free tier

The AGPL keeps the community edition open: anyone who improves the
client and offers it as a service must share those improvements. That
is exactly the trade an individual user wants and exactly the pressure
that makes the enterprise license (with support, audit and compliance
tooling, and no copyleft obligations) a fair deal for organizations.
