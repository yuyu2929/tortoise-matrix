"""Quick smoke check: a 2-second-latency server must not slow us down."""
import asyncio
import sys
import time

sys.path.insert(0, "D:/有用/tortoise-matrix")

from tortoise import MockMatrixServer, TortoiseClient  # noqa: E402


async def main() -> None:
    server = MockMatrixServer(latency=2.0, jitter=0.5)
    async with TortoiseClient(transport=server, local_user="@me:localhost") as c:
        await c.join_room("!room:localhost", name="Slow Server Test")

        t0 = time.perf_counter()
        ev = await c.send_message("!room:localhost", "@me:localhost", "hello slow server")
        t1 = time.perf_counter()
        print(f"[send]      returned in {(t1 - t0) * 1000:7.2f} ms  status={ev.status}")

        t0 = time.perf_counter()
        msgs = await c.get_messages("!room:localhost")
        t1 = time.perf_counter()
        print(f"[read]      returned in {(t1 - t0) * 1000:7.2f} ms  count={len(msgs)}")
        print(f"[read]      body={msgs[-1]['body']!r} status={msgs[-1]['status']}")

        # The server takes ~2s per call. Prove reads stay instant meanwhile.
        t0 = time.perf_counter()
        for _ in range(50):
            await c.get_messages("!room:localhost")
        t1 = time.perf_counter()
        print(f"[read x50]  returned in {(t1 - t0) * 1000:7.2f} ms total")

        acked = None
        for _ in range(100):
            await asyncio.sleep(0.1)
            msgs = await c.get_messages("!room:localhost")
            if msgs and msgs[-1]["event_id"]:
                acked = msgs[-1]
                break
        if acked is None:
            print("[after ack] TIMEOUT - message never acknowledged")
        else:
            print(f"[after ack] status={acked['status']} event_id={acked['event_id'][:12]}...")

        print("[status]", await c.status())


asyncio.run(main())
