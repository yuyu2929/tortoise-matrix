"""A 30-line tour of the public API.

Run: python examples/quickstart.py
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tortoise import MockMatrixServer, TortoiseClient  # noqa: E402

ROOM = "!tour:localhost"


async def main() -> None:
    # 3-second-latency server: worst-case matrix.org on a bad day.
    async with TortoiseClient(
        transport=MockMatrixServer(latency=3.0, jitter=0.5),
        local_user="@me:localhost",
    ) as c:
        await c.join_room(ROOM, name="Slow Server Stress Test")

        # 1. Optimistic send: visible instantly, acknowledged later.
        ev = await c.send_message(ROOM, "@me:localhost", "hello slow server")
        print("visible now: ", ev.status)            # pending

        # 2. Reads are always local and instant.
        msgs = await c.get_messages(ROOM)
        print("local view:  ", [(m["body"], m["status"]) for m in msgs])

        # 3. Subscribe to the lifecycle: pending → sent.
        done = asyncio.Event()

        def on_ack(e):
            if e.local_echo_id == ev.local_echo_id:
                done.set()

        c.subscribe("MessageAcknowledged", on_ack)
        await asyncio.wait_for(done.wait(), timeout=15.0)
        msgs = await c.get_messages(ROOM)
        row = next(m for m in msgs if m["local_echo_id"] == ev.local_echo_id)
        print("acknowledged:", row["status"], row["event_id"])

        # 4. Health snapshot.
        print("status:      ", await c.status())


if __name__ == "__main__":
    asyncio.run(main())
